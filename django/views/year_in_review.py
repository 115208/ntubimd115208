"""年度回顧（Wrapped 式全螢幕輪播）。

把某一年份的真實紀錄（孕期／產檢／心情／寶寶紀錄／里程碑／照片／待辦）
聚合成一串「故事卡」，交給前端逐張自動播放；沒有資料的卡片直接略過，
不塞任何示範數據。統計口徑與權限閘門沿用第三版歷史回顧。
"""
import datetime
from collections import Counter

from django.shortcuts import redirect, render
from django.utils import timezone

from core.models import (
    BabyStatus,
    CareRecord,
    PregnancyCase,
    PregnancyRecord,
    Prenatalrecord,
    Userfeeling,
)
from views.history_review import resolve_view_permissions
from views.history_review_v3 import (
    FEELING_EMOJI_MAP,
    WEEKDAY_MAP,
    _baby_record_scope,
    _format_photo_url,
    _mom_scope_uid,
)
from views.pregnancycase import (
    baby_switcher,
    get_lmp_date,
    resolve_active_baby,
    resolve_active_pregnancy_case,
    sync_active_selection_from_request,
)
from views.session_utils import get_current_user_profile

MONTH_LABELS = {m: f'{m} 月' for m in range(1, 13)}
MAX_COLLAGE_PHOTOS = 6
MAX_MILESTONES = 6


def _year_bounds(year):
    return datetime.date(year, 1, 1), datetime.date(year, 12, 31)


def _to_date(value):
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    return value


def _collect_year_data(current_user, pregnancy_case, active_baby, year,
                       can_view_mom, can_view_baby):
    """把該年份會用到的 ORM 資料一次撈齊，回傳純 Python 結構，方便組卡片與測試。"""
    target_uid = _mom_scope_uid(current_user, pregnancy_case)
    start, end = _year_bounds(year)

    data = {
        'preg_records': [],
        'prenatals': [],
        'feelings': [],
        'baby_records': [],
        'milestones': [],
        'cares': [],
    }

    if can_view_mom:
        data['preg_records'] = list(
            PregnancyRecord.objects.filter(
                user_id=target_uid, check_date__range=(start, end)
            ).order_by('check_date')
        )
        data['prenatals'] = list(
            Prenatalrecord.objects.filter(
                pregnancyrecord__user_id=target_uid,
                pregnancyrecord__check_date__range=(start, end),
            ).select_related('pregnancyrecord').order_by('pregnancyrecord__check_date')
        )
        data['feelings'] = list(
            Userfeeling.objects.filter(
                pregnancyrecord__user_id=target_uid,
                pregnancyrecord__check_date__range=(start, end),
            ).select_related('feeling', 'pregnancyrecord')
        )

    if can_view_baby:
        baby_scope = _baby_record_scope(current_user, pregnancy_case, active_baby)
        data['baby_records'] = list(
            baby_scope.filter(date__range=(start, end)).select_related('baby').order_by('date')
        )
        if active_baby:
            data['milestones'] = list(
                BabyStatus.objects.filter(
                    babyrecord__baby=active_baby,
                    babyrecord__date__range=(start, end),
                )
                .select_related('babygrowthmap', 'babyrecord')
                .order_by('babyrecord__date', 'babygrowthmap__timecourse')
            )

    cares = (
        CareRecord.objects.filter(pregnancycase=pregnancy_case)
        if pregnancy_case
        else CareRecord.objects.filter(pregnancycase__user=current_user)
    )
    data['cares'] = list(cares.filter(recordtime__date__range=(start, end)))
    return data


def _available_years(current_user, pregnancy_case, active_baby, can_view_mom, can_view_baby):
    """有任何紀錄的年份（新到舊），供封面切換年份用。"""
    target_uid = _mom_scope_uid(current_user, pregnancy_case)
    years = set()
    if can_view_mom:
        years.update(
            d.year for d in PregnancyRecord.objects.filter(user_id=target_uid)
            .values_list('check_date', flat=True) if d
        )
    if can_view_baby:
        years.update(
            d.year for d in _baby_record_scope(current_user, pregnancy_case, active_baby)
            .values_list('date', flat=True) if d
        )
    return sorted(years, reverse=True)


def _build_slides(year, data, *, today, subject_name, preg_case, active_baby):
    """由聚合資料組出故事卡。每張卡片都有 kind / theme / caption，
    前端只負責播放；文案為規則式產生，之後可換成 LLM 生成。"""
    start, end = _year_bounds(year)
    slides = []

    all_dates = (
        [r.check_date for r in data['preg_records']]
        + [r.date for r in data['baby_records']]
        + [_to_date(c.recordtime) for c in data['cares'] if c.recordtime]
    )
    all_dates = [d for d in all_dates if d]
    total_records = (
        len(data['preg_records']) + len(data['feelings'])
        + len(data['baby_records']) + len(data['cares'])
    )
    photo_items = []
    for b in data['baby_records']:
        if b.photo:
            photo_items.append({'url': _format_photo_url(b.photo), 'date': b.date, 'label': '寶寶紀錄'})
    for p in data['prenatals']:
        if p.photo and p.pregnancyrecord:
            photo_items.append({
                'url': _format_photo_url(p.photo),
                'date': p.pregnancyrecord.check_date,
                'label': '超音波',
            })
    photo_items.sort(key=lambda x: x['date'])

    # 1. 封面
    slides.append({
        'kind': 'cover',
        'theme': 'violet',
        'title': f'{year} 年度回顧',
        'subtitle': f'{subject_name} 的這一年',
        'caption': '輕觸畫面，一起回顧這一年的點點滴滴',
    })

    if total_records == 0 and not photo_items:
        slides.append({
            'kind': 'empty',
            'theme': 'mist',
            'title': '這一年還沒有紀錄',
            'caption': '從今天開始記錄，明年的回顧就會很精彩',
        })
        return slides

    # 2. 陪伴天數（只算落在該年內、且已經開始陪伴的日子）
    anchor = None
    if preg_case:
        anchor = get_lmp_date(preg_case)
    if not anchor and active_baby and active_baby.birthdaytime:
        anchor = _to_date(active_baby.birthdaytime)
    if not anchor and all_dates:
        anchor = min(all_dates)
    if anchor:
        range_start = max(anchor, start)
        range_end = min(today, end)
        days = (range_end - range_start).days + 1
        if days > 0:
            slides.append({
                'kind': 'number',
                'theme': 'sunrise',
                'icon': '📅',
                'number': days,
                'unit': '天',
                'title': '這一年我們一起走過',
                'caption': '每一天都算數，謝謝你一直都在',
            })

    # 3. 紀錄總數與組成
    breakdown = [
        ('孕期紀錄', len(data['preg_records'])),
        ('心情', len(data['feelings'])),
        ('寶寶紀錄', len(data['baby_records'])),
        ('待辦', len(data['cares'])),
    ]
    breakdown = [{'label': k, 'count': v} for k, v in breakdown if v]
    if total_records:
        slides.append({
            'kind': 'number',
            'theme': 'violet',
            'icon': '✍️',
            'number': total_records,
            'unit': '筆紀錄',
            'title': '你為這個家寫下了',
            'caption': '細心留下的每一筆，都是未來最珍貴的回憶',
            'chips': breakdown,
        })

    # 4. 最勤勞的月份與最常紀錄的星期
    if all_dates:
        month_counter = Counter(d.month for d in all_dates)
        best_month, best_month_count = month_counter.most_common(1)[0]
        weekday_counter = Counter(d.weekday() for d in all_dates)
        best_weekday, _ = weekday_counter.most_common(1)[0]
        months = [
            {'label': MONTH_LABELS[m], 'count': month_counter.get(m, 0),
             'pct': round(month_counter.get(m, 0) / best_month_count * 100)}
            for m in range(1, 13)
        ]
        slides.append({
            'kind': 'month',
            'theme': 'ocean',
            'icon': '🗓️',
            'title': f'{MONTH_LABELS[best_month]}是最用心的月份',
            'number': best_month_count,
            'unit': '筆',
            'caption': f'你最常在{WEEKDAY_MAP[best_weekday]}留下紀錄',
            'months': months,
        })

    # 5. 照片拼貼
    if photo_items:
        slides.append({
            'kind': 'photos',
            'theme': 'peach',
            'icon': '📷',
            'number': len(photo_items),
            'unit': '張照片',
            'title': '定格了',
            'caption': '有些瞬間，只會發生一次',
            'photos': photo_items[-MAX_COLLAGE_PHOTOS:],
        })

    # 6. 寶寶成長（該年內第一筆與最後一筆有數值的紀錄）
    if active_baby and data['baby_records']:
        weights = [(r.date, r.weight) for r in data['baby_records'] if r.weight]
        heights = [(r.date, r.height) for r in data['baby_records'] if r.height]
        metrics = []
        if len(weights) >= 2:
            metrics.append({
                'label': '體重', 'unit': 'kg',
                'from': weights[0][1], 'to': weights[-1][1],
                'delta': round(weights[-1][1] - weights[0][1], 2),
            })
        if len(heights) >= 2:
            metrics.append({
                'label': '身高', 'unit': 'cm',
                'from': heights[0][1], 'to': heights[-1][1],
                'delta': round(heights[-1][1] - heights[0][1], 1),
            })
        if metrics:
            slides.append({
                'kind': 'growth',
                'theme': 'mint',
                'icon': '🌱',
                'title': f'{active_baby.name or "寶寶"} 長大了',
                'caption': '一點一點，悄悄地就長這麼大了',
                'metrics': metrics,
            })

    # 7. 里程碑
    if data['milestones']:
        items = []
        seen = set()
        for st in data['milestones']:
            if not st.babygrowthmap or st.babygrowthmap_id in seen:
                continue
            seen.add(st.babygrowthmap_id)
            rec = st.babyrecord
            items.append({
                'title': st.babygrowthmap.growthrecord,
                'date_str': rec.date.strftime('%m/%d') if (rec and rec.date) else '',
            })
        if items:
            slides.append({
                'kind': 'milestones',
                'theme': 'gold',
                'icon': '⭐',
                'number': len(items),
                'unit': '個里程碑',
                'title': '今年解鎖了',
                'caption': '每個第一次，都值得被記住',
                'items': items[:MAX_MILESTONES],
                'more': max(0, len(items) - MAX_MILESTONES),
            })

    # 8. 心情
    if data['feelings']:
        names = [
            f.feeling.feeling_name for f in data['feelings']
            if f.feeling and getattr(f.feeling, 'feeling_name', None)
        ]
        if names:
            counter = Counter(names)
            top_name, top_count = counter.most_common(1)[0]
            total = sum(counter.values())
            slides.append({
                'kind': 'mood',
                'theme': 'rose',
                'emoji': FEELING_EMOJI_MAP.get(top_name, '📝'),
                'title': f'今年最常出現的心情是「{top_name}」',
                'number': top_count,
                'unit': '次',
                'caption': '不論好壞，每種心情都是這段旅程的一部分',
                'moods': [
                    {'name': n, 'emoji': FEELING_EMOJI_MAP.get(n, '📝'),
                     'count': c, 'pct': round(c / total * 100)}
                    for n, c in counter.most_common(3)
                ],
            })

    # 9. 產檢與待辦
    care_done = sum(1 for c in data['cares'] if c.state)
    if data['prenatals'] or care_done:
        facts = []
        if data['prenatals']:
            facts.append({'icon': '🩺', 'number': len(data['prenatals']), 'label': '次產檢'})
        if care_done:
            facts.append({'icon': '✅', 'number': care_done, 'label': '件待辦完成'})
        slides.append({
            'kind': 'facts',
            'theme': 'lavender',
            'title': '默默完成的事',
            'caption': '照顧好自己，也是照顧好寶寶',
            'facts': facts,
        })

    # 10. 結尾總結
    summary = []
    if total_records:
        summary.append({'number': total_records, 'label': '筆紀錄'})
    if photo_items:
        summary.append({'number': len(photo_items), 'label': '張照片'})
    if data['milestones']:
        summary.append({
            'number': len({s.babygrowthmap_id for s in data['milestones']}),
            'label': '個里程碑',
        })
    slides.append({
        'kind': 'finale',
        'theme': 'night',
        'title': f'謝謝你的 {year}',
        'subtitle': f'{subject_name} 的年度回顧',
        'caption': '明年，我們繼續一起記錄',
        'summary': summary,
    })
    return slides


def year_in_review(request):
    """年度回顧：全螢幕故事卡自動輪播，可存成圖片分享。"""
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    sync_active_selection_from_request(request, current_user)
    pregnancy_case = resolve_active_pregnancy_case(request, current_user)
    active_baby = resolve_active_baby(request, current_user)
    switcher_data = baby_switcher(request)
    today = timezone.localdate()

    can_view_mom, can_view_baby, permission_notices = resolve_view_permissions(
        current_user, pregnancy_case
    )

    available_years = _available_years(
        current_user, pregnancy_case, active_baby, can_view_mom, can_view_baby
    )
    try:
        year = int(request.GET.get('year', ''))
    except (TypeError, ValueError):
        year = available_years[0] if available_years else today.year
    if year not in available_years:
        available_years = sorted(set(available_years) | {year}, reverse=True)

    preg_case = pregnancy_case
    if not preg_case and active_baby and getattr(active_baby, 'pregnancycase', None):
        preg_case = active_baby.pregnancycase
    if not preg_case:
        preg_case = PregnancyCase.objects.filter(user=current_user).first()

    data = _collect_year_data(
        current_user, pregnancy_case, active_baby, year, can_view_mom, can_view_baby
    )
    subject_name = (
        active_baby.name if (active_baby and active_baby.name) else current_user.name
    ) or '我們'
    slides = _build_slides(
        year, data, today=today, subject_name=subject_name,
        preg_case=preg_case, active_baby=active_baby,
    )

    context = {
        'year': year,
        'available_years': available_years,
        'slides': slides,
        'slide_count': len(slides),
        'subject_name': subject_name,
        'active_v3_tab': 'year_in_review',
        'can_view_mom': can_view_mom,
        'can_view_baby': can_view_baby,
        'permission_notices': permission_notices,
    }
    context.update(switcher_data)
    return render(request, 'history/v3_year_in_review.html', context)
