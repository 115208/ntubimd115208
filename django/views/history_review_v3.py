import datetime
from django.db.models import Count, Q
from django.shortcuts import render, redirect
from django.utils import timezone

from core.models import (
    BabyGrowthMap,
    BabyInformation,
    BabyRecord,
    BabyStatus,
    CareRecord,
    FamilyMember,
    Feeling,
    PhysicalCondition,
    PregnancyCase,
    PregnancyRecord,
    Prenatalrecord,
    QAMessage,
    Userfeeling,
    Userphysicalcondition,
    UserProfile,
)
from views.pregnancycase import (
    baby_switcher,
    get_gestation_parts,
    get_lmp_date,
    is_pregnancy_ongoing,
    resolve_active_baby,
    resolve_active_pregnancy_case,
    sync_active_selection_from_request,
)
from views.session_utils import get_current_user_profile
# 權限閘門與第一版共用同一份實作，避免兩版規則漂移
from views.history_review import resolve_view_permissions

FEELING_EMOJI_MAP = {
    '快樂': '😊',
    '幸福': '🥰',
    '開心': '😆',
    '心跳加速': '😳',
    '還好': '😐',
    '煩': '😮‍💨',
    '怒': '😡',
    '累': '😫',
    '不安': '😰',
    '難受': '😭',
    '不舒服': '🤢',
}

WEEKDAY_MAP = {
    0: '週一',
    1: '週二',
    2: '週三',
    3: '週四',
    4: '週五',
    5: '週六',
    6: '週日',
}


def _mom_scope_uid(current_user, pregnancy_case):
    """媽媽相關資料（孕期紀錄／產檢／心情）一律以個案擁有者為基準，
    與第一版歷史回顧、/pregnancyrecord/ 的口徑一致。"""
    return pregnancy_case.user_id if pregnancy_case else current_user.user_id


def _baby_record_scope(current_user, pregnancy_case, active_baby):
    """寶寶相關資料的查詢範圍：優先用切換器選到的寶寶，其次整個個案。"""
    if active_baby:
        return BabyRecord.objects.filter(baby=active_baby)
    if pregnancy_case:
        return BabyRecord.objects.filter(baby__pregnancycase=pregnancy_case)
    return BabyRecord.objects.filter(baby__pregnancycase__user=current_user)


def _calc_stats(current_user, pregnancy_case, active_baby, today,
                can_view_mom=True, can_view_baby=True):
    """計算陪伴天數（從懷孕起算）、總照片數量、媽媽紀錄筆數、小孩紀錄筆數 (純真實 ORM 數據)。

    統計口徑已與第一版統一：以 pregnancy_case（及切換器選到的寶寶）為基準，
    不再用登入者自己的 user 撈全部資料；沒有檢視權限的類別一律計為 0。
    """
    days_accompanied = 0
    preg_case = pregnancy_case
    if not preg_case and active_baby and getattr(active_baby, 'pregnancycase', None):
        preg_case = active_baby.pregnancycase
    if not preg_case:
        preg_case = PregnancyCase.objects.filter(user=current_user).first()

    target_uid = _mom_scope_uid(current_user, preg_case)

    if preg_case:
        lmp = get_lmp_date(preg_case)
        if lmp:
            delta = today - lmp
            days_accompanied = max(0, delta.days)
    else:
        first_preg = (
            PregnancyRecord.objects.filter(user_id=target_uid)
            .order_by('check_date')
            .first()
        )
        if first_preg and first_preg.check_date:
            days_accompanied = max(0, (today - first_preg.check_date).days)
        elif active_baby and active_baby.birthdaytime:
            birth_date = (
                active_baby.birthdaytime.date()
                if hasattr(active_baby.birthdaytime, 'date')
                else active_baby.birthdaytime
            )
            if birth_date:
                days_accompanied = max(0, (today - birth_date).days + 280)

    baby_scope = _baby_record_scope(current_user, preg_case, active_baby)

    if can_view_mom:
        total_ultrasounds = Prenatalrecord.objects.filter(
            pregnancyrecord__user_id=target_uid, photo__isnull=False
        ).exclude(photo='').count()
        mom_preg_records = PregnancyRecord.objects.filter(user_id=target_uid).count()
        mom_feelings = Userfeeling.objects.filter(pregnancyrecord__user_id=target_uid).count()
        mom_record_count = mom_preg_records + mom_feelings
    else:
        total_ultrasounds = 0
        mom_record_count = 0

    if can_view_baby:
        total_baby_photos = baby_scope.filter(photo__isnull=False).exclude(photo='').count()
        baby_record_count = baby_scope.count()
    else:
        total_baby_photos = 0
        baby_record_count = 0

    total_photos = total_ultrasounds + total_baby_photos

    # 過去這裡沒有任何使用者條件，顯示的是全站所有人的 AI 問答總數
    ai_qa_count = QAMessage.objects.filter(
        qa_conversation__user_id=current_user, role__in=['assistant', 'ai']
    ).count()

    return {
        'days_accompanied': days_accompanied,
        'total_photos': total_photos,
        'mom_record_count': mom_record_count,
        'baby_record_count': baby_record_count,
        'ai_qa_count': ai_qa_count,
    }


def _format_photo_url(photo_str):
    if not photo_str:
        return None
    photo_str = str(photo_str).strip()
    if photo_str.startswith('http://') or photo_str.startswith('https://') or photo_str.startswith('/'):
        return photo_str
    return f'/media/{photo_str}'


def v3_timeline(request):
    """第三版時光軸：一條乾淨時間線 + 圓形節點 + Hover popover卡片 + 時間/類型篩選"""
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
    target_uid = _mom_scope_uid(current_user, pregnancy_case)

    stats = _calc_stats(
        current_user, pregnancy_case, active_baby, today, can_view_mom, can_view_baby
    )
    filter_type = request.GET.get('filter', 'all')
    time_range = request.GET.get('time_range', 'all')

    events = []

    # 1. 產檢紀錄（mom_records 未開放時完全不查詢）
    prenatals = (
        Prenatalrecord.objects.filter(
            pregnancyrecord__user_id=target_uid
        ).select_related('pregnancyrecord')
        if can_view_mom
        else Prenatalrecord.objects.none()
    )

    for p in prenatals:
        rec = p.pregnancyrecord
        dt = rec.check_date if rec else None
        if not dt:
            continue

        weight_str = f"{rec.weight} kg" if rec and rec.weight else "-"
        bp_str = f"{p.sbp or '-'}/{p.dbp or '-'} mmHg"

        events.append({
            'id': f'prenatal_{p.prenatalrecord_id}',
            'date': dt,
            'date_str': dt.strftime('%Y-%m-%d'),
            'type': 'prenatal',
            'type_label': '產檢',
            'badge_color': 'bg-[#EDE5F5] text-[#65518a] border-[#E8E0EF]',
            'dot_color': 'bg-[#65518a]',
            'title': f'產檢紀錄',
            'content': f'體重: {weight_str} | 血壓: {bp_str}',
            'photo': _format_photo_url(p.photo),
            'creator': current_user.name,
            'gestation_weeks': None,
            'note': rec.record if rec else '',
        })

    # 2. 心情紀錄（mom_records 未開放時完全不查詢）
    feelings = (
        Userfeeling.objects.filter(
            pregnancyrecord__user_id=target_uid
        ).select_related('feeling', 'pregnancyrecord')
        if can_view_mom
        else Userfeeling.objects.none()
    )

    for f in feelings:
        rec = f.pregnancyrecord
        dt = rec.check_date if rec else None
        if not dt:
            continue

        f_name = f.feeling.feeling_name if (f.feeling and hasattr(f.feeling, 'feeling_name')) else '心情'
        emoji = FEELING_EMOJI_MAP.get(f_name, '📝')

        events.append({
            'id': f'feeling_{f.userfeeling_id}',
            'date': dt,
            'date_str': dt.strftime('%Y-%m-%d'),
            'type': 'feeling',
            'type_label': '心情',
            'badge_color': 'bg-[#EDE5F5] text-[#65518a] border-[#E8E0EF]',
            'dot_color': 'bg-[#65518a]',
            'title': f'{emoji} 心情紀錄：{f_name}',
            'content': rec.record if rec and rec.record else '分享今日好心情',
            'photo': None,
            'creator': current_user.name,
            'gestation_weeks': None,
            'note': '',
        })

    # 3. 待辦提醒（以個案為範圍，與首頁 views/index.py 一致）
    cares = (
        CareRecord.objects.filter(pregnancycase=pregnancy_case)
        if pregnancy_case
        else CareRecord.objects.filter(user=current_user)
    )
    for c in cares:
        dt = c.recordtime.date() if c.recordtime else None
        if not dt:
            continue
        events.append({
            'id': f'care_{c.carerecord_id}',
            'date': dt,
            'date_str': dt.strftime('%Y-%m-%d'),
            'type': 'task',
            'type_label': '待辦',
            'badge_color': 'bg-[#EDE5F5] text-[#65518a] border-[#E8E0EF]',
            'dot_color': 'bg-[#65518a]',
            'title': f'待辦：{c.content or "待辦清單"}',
            'content': f'狀態: {"已完成" if c.state else "未完成"}',
            'photo': None,
            'creator': current_user.name,
            'gestation_weeks': None,
            'note': '',
        })

    # 4. 寶寶紀錄（依切換器選到的寶寶過濾；baby_records 未開放時完全不查詢）
    baby_recs = (
        _baby_record_scope(current_user, pregnancy_case, active_baby).select_related('baby')
        if can_view_baby
        else BabyRecord.objects.none()
    )

    for b in baby_recs:
        dt = b.date
        if not dt:
            continue
        events.append({
            'id': f'baby_{b.babyrecord_id}',
            'date': dt,
            'date_str': dt.strftime('%Y-%m-%d'),
            'type': 'baby',
            'type_label': '寶寶',
            'badge_color': 'bg-[#EDE5F5] text-[#65518a] border-[#E8E0EF]',
            'dot_color': 'bg-[#65518a]',
            'title': f'{b.baby.name if b.baby else "寶寶"} 成長紀錄',
            'content': f'身高: {b.height or "-"} cm | 體重: {b.weight or "-"} kg',
            'photo': _format_photo_url(b.photo),
            'creator': current_user.name,
            'gestation_weeks': None,
            'note': b.record or '',
        })

    # 排序 (降序)
    events.sort(key=lambda x: x['date'], reverse=True)

    # 可選年份
    available_years = sorted(list({e['date'].year for e in events if e.get('date')}), reverse=True)

    # 類型篩選 (filter_type: 'prenatal', 'feeling', 'task', 'baby', 'all')
    if filter_type and filter_type != 'all':
        events = [e for e in events if e['type'] == filter_type]

    # 時間範圍篩選 (time_range)
    if time_range and time_range != 'all':
        if time_range == '1m':
            cutoff = today - datetime.timedelta(days=30)
            events = [e for e in events if e['date'] >= cutoff]
        elif time_range == '3m':
            cutoff = today - datetime.timedelta(days=90)
            events = [e for e in events if e['date'] >= cutoff]
        elif time_range == '6m':
            cutoff = today - datetime.timedelta(days=180)
            events = [e for e in events if e['date'] >= cutoff]
        elif time_range == '1y':
            cutoff = today - datetime.timedelta(days=365)
            events = [e for e in events if e['date'] >= cutoff]
        elif time_range.isdigit():
            target_year = int(time_range)
            events = [e for e in events if e['date'].year == target_year]

    # 5. 身體狀況分布統計 (百分比)
    user_physicals = (
        Userphysicalcondition.objects.filter(pregnancyrecord__user_id=target_uid)
        .values('physicalcondition__physicalcondition_name')
        .annotate(cnt=Count('userphysicalcondition_id'))
        .order_by('-cnt')
        if can_view_mom
        else Userphysicalcondition.objects.none()
    )
    total_physical_count = sum(item['cnt'] for item in user_physicals)
    physical_stats = []

    color_palette = [
        {'bg': 'bg-[#65518a]', 'hex': '#65518a'},
        {'bg': 'bg-[#8064a2]', 'hex': '#8064a2'},
        {'bg': 'bg-[#9b7bbd]', 'hex': '#9b7bbd'},
        {'bg': 'bg-[#c3aed6]', 'hex': '#c3aed6'},
        {'bg': 'bg-[#d6beff]', 'hex': '#d6beff'},
    ]

    if total_physical_count > 0:
        for idx, item in enumerate(user_physicals[:4]):
            name = item['physicalcondition__physicalcondition_name'] or '健康'
            cnt = item['cnt']
            pct = round((cnt / total_physical_count) * 100)
            color = color_palette[idx % len(color_palette)]
            physical_stats.append({
                'name': name,
                'count': cnt,
                'percentage': pct,
                'color_bg': color['bg'],
                'color_hex': color['hex'],
            })

        if len(user_physicals) > 4:
            other_cnt = sum(item['cnt'] for item in user_physicals[4:])
            other_pct = max(0, 100 - sum(s['percentage'] for s in physical_stats))
            physical_stats.append({
                'name': '其他症狀',
                'count': other_cnt,
                'percentage': other_pct,
                'color_bg': 'bg-[#e3e3df]',
                'color_hex': '#e3e3df',
            })
    else:
        # 沒有身體狀況紀錄就是沒有：不再塞入虛構的孕吐／腰痠背痛比例
        physical_stats = []
        total_physical_count = 0

    # 6. 超音波相片資料 (供影片播放器 Template 使用)
    ultrasound_photos = []
    prenatals_with_photo = (
        Prenatalrecord.objects.filter(
            pregnancyrecord__user_id=target_uid, photo__isnull=False
        ).exclude(photo='').select_related('pregnancyrecord')
        if can_view_mom
        else Prenatalrecord.objects.none()
    )

    for p in prenatals_with_photo:
        rec = p.pregnancyrecord
        dt = rec.check_date if rec else None
        ultrasound_photos.append({
            'url': _format_photo_url(p.photo),
            'date_str': dt.strftime('%Y-%m-%d') if dt else '未知日期',
            'weight': rec.weight if rec and rec.weight else None,
            'bp': f"{p.sbp or '-'}/{p.dbp or '-'} mmHg" if (p.sbp or p.dbp) else None,
            'note': rec.record if (rec and rec.record) else '',
        })

    # 沒有超音波照就顯示空狀態，不再用 Unsplash 的網路圖片冒充使用者的產檢照

    mode = request.GET.get('mode', 'video')

    context = {
        'events': events,
        'filter_type': filter_type,
        'time_range': time_range,
        'available_years': available_years,
        'stats': stats,
        'physical_stats': physical_stats,
        'total_physical_count': total_physical_count,
        'has_physical_stats': bool(physical_stats),
        'ultrasound_photos': ultrasound_photos,
        'has_ultrasound_photos': bool(ultrasound_photos),
        'mode': mode,
        'active_v3_tab': 'timeline',
        'can_view_mom': can_view_mom,
        'can_view_baby': can_view_baby,
        'permission_notices': permission_notices,
    }
    context.update(switcher_data)
    return render(request, 'history/v3_timeline.html', context)


def v3_memory_wall(request):
    """第三版相簿牆：時間分類與胎數分類"""
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    sync_active_selection_from_request(request, current_user)
    pregnancy_case = resolve_active_pregnancy_case(request, current_user)
    active_baby = resolve_active_baby(request, current_user)
    switcher_data = baby_switcher(request)
    group_mode = request.GET.get('group', 'time')  # 'time' 或 'gestation'

    can_view_mom, can_view_baby, permission_notices = resolve_view_permissions(
        current_user, pregnancy_case
    )
    target_uid = _mom_scope_uid(current_user, pregnancy_case)

    photos = []

    # 1. 超音波照片（mom_records 未開放時完全不查詢）
    prenatals = (
        Prenatalrecord.objects.filter(
            pregnancyrecord__user_id=target_uid, photo__isnull=False
        ).exclude(photo='').select_related('pregnancyrecord')
        if can_view_mom
        else Prenatalrecord.objects.none()
    )

    case_obj = pregnancy_case or PregnancyCase.objects.filter(user=current_user).first()
    prenatal_baby_count = (
        BabyInformation.objects.filter(pregnancycase=case_obj).count()
        if case_obj
        else 1
    )

    for p in prenatals:
        dt = p.pregnancyrecord.check_date if p.pregnancyrecord else None
        baby_count = prenatal_baby_count

        if baby_count == 1:
            gest_type = '單胞胎'
        elif baby_count == 2:
            gest_type = '雙胞胎'
        elif baby_count >= 3:
            gest_type = f'{baby_count}胞胎'
        else:
            gest_type = '單胞胎'

        photos.append({
            'url': _format_photo_url(p.photo),
            'title': '超音波照片',
            'date': dt,
            'date_str': dt.strftime('%Y-%m-%d') if dt else '未知日期',
            'year_month': dt.strftime('%Y 年 %m 月') if dt else '未知時間',
            'gestation_type': gest_type,
        })

    # 2. 寶寶照片（依切換器選到的寶寶過濾；baby_records 未開放時完全不查詢）
    baby_recs = (
        _baby_record_scope(current_user, pregnancy_case, active_baby)
        .filter(photo__isnull=False)
        .exclude(photo='')
        .select_related('baby', 'baby__pregnancycase')
        if can_view_baby
        else BabyRecord.objects.none()
    )

    for b in baby_recs:
        dt = b.date
        pcase = b.baby.pregnancycase if b.baby else None
        baby_count = (
            BabyInformation.objects.filter(pregnancycase=pcase).count()
            if pcase
            else 1
        )

        if baby_count == 1:
            gest_type = '單胞胎'
        elif baby_count == 2:
            gest_type = '雙胞胎'
        elif baby_count >= 3:
            gest_type = f'{baby_count}胞胎'
        else:
            gest_type = '單胞胎'

        photos.append({
            'url': _format_photo_url(b.photo),
            'title': f'{b.baby.name if b.baby else "寶寶"} 照片',
            'date': dt,
            'date_str': dt.strftime('%Y-%m-%d') if dt else '未知日期',
            'year_month': dt.strftime('%Y 年 %m 月') if dt else '未知時間',
            'gestation_type': gest_type,
        })

    # 排序：用真正的日期欄位，不要用格式化後的字串（未知日期排到最後）
    photos.sort(key=lambda x: (x['date'] is not None, x['date'] or datetime.date.min), reverse=True)

    grouped_photos = {}
    if group_mode == 'gestation':
        for item in photos:
            g = item['gestation_type']
            grouped_photos.setdefault(g, []).append(item)
    else:
        for item in photos:
            ym = item['year_month']
            grouped_photos.setdefault(ym, []).append(item)

    context = {
        'grouped_photos': grouped_photos,
        'group_mode': group_mode,
        'total_photo_count': len(photos),
        'active_v3_tab': 'memory_wall',
        'can_view_mom': can_view_mom,
        'can_view_baby': can_view_baby,
        'permission_notices': permission_notices,
    }
    context.update(switcher_data)
    return render(request, 'history/v3_memory_wall.html', context)


def v3_baby_growth(request):
    """第三版紀念冊：統計卡 + 由真實資料組出的成長旅程節點。

    舊版模板整頁都是寫死的示範節點（第一次聽見心跳 2023.08.15 …），
    對健康類系統而言等同把假資料當成真實紀錄呈現，已全部改成真實資料，
    沒有資料就顯示空狀態。
    """
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
    target_uid = _mom_scope_uid(current_user, pregnancy_case)

    stats = _calc_stats(
        current_user, pregnancy_case, active_baby, today, can_view_mom, can_view_baby
    )

    journey_nodes = []

    # 1. 第一筆孕期紀錄
    if can_view_mom:
        first_preg = (
            PregnancyRecord.objects.filter(user_id=target_uid, check_date__isnull=False)
            .order_by('check_date')
            .first()
        )
        if first_preg:
            journey_nodes.append({
                'icon': '📝',
                'title': '第一筆孕期紀錄',
                'badge': '孕期紀錄',
                'badge_class': 'text-[#65518a] bg-[#EDE5F5] border-[#E8E0EF]',
                'date_str': first_preg.check_date.strftime('%Y.%m.%d'),
                'note': (first_preg.record or '').strip(),
            })

        first_scan = (
            Prenatalrecord.objects.filter(
                pregnancyrecord__user_id=target_uid,
                pregnancyrecord__check_date__isnull=False,
            )
            .select_related('pregnancyrecord')
            .order_by('pregnancyrecord__check_date')
            .first()
        )
        if first_scan and first_scan.pregnancyrecord:
            journey_nodes.append({
                'icon': '🩺',
                'title': '第一次產檢紀錄',
                'badge': '健康檢查',
                'badge_class': 'text-[#65518a] bg-[#EDE5F5] border-[#E8E0EF]',
                'date_str': first_scan.pregnancyrecord.check_date.strftime('%Y.%m.%d'),
                'note': (first_scan.pregnancyrecord.record or '').strip(),
            })

    # 2. 寶寶誕生
    if can_view_baby and active_baby and active_baby.birthdaytime:
        birth_dt = active_baby.birthdaytime
        journey_nodes.append({
            'icon': '👶',
            'title': f'{active_baby.name or "寶寶"} 誕生',
            'badge': '圓滿誕生',
            'badge_class': 'text-[#65518a] bg-[#EDE5F5] border-[#E8E0EF]',
            'date_str': birth_dt.strftime('%Y.%m.%d'),
            'note': (active_baby.production_method or '').strip(),
        })

    # 3. 寶寶已達成的成長里程碑（真實 BabyStatus 資料）
    if can_view_baby and active_baby:
        achieved = (
            BabyStatus.objects.filter(babyrecord__baby=active_baby)
            .select_related('babygrowthmap', 'babyrecord')
            .order_by('babygrowthmap__timecourse')
        )
        for st in achieved:
            if not st.babygrowthmap:
                continue
            rec = st.babyrecord
            journey_nodes.append({
                'icon': '⭐',
                'title': st.babygrowthmap.growthrecord,
                'badge': f'{st.babygrowthmap.timecourse} 個月',
                'badge_class': 'text-[#65518a] bg-[#EDE5F5] border-[#E8E0EF]',
                'date_str': rec.date.strftime('%Y.%m.%d') if (rec and rec.date) else '',
                'note': (rec.record or '').strip() if rec else '',
            })

    context = {
        'stats': stats,
        'active_v3_tab': 'baby_growth',
        'active_baby': active_baby,
        'journey_nodes': journey_nodes,
        'has_journey': bool(journey_nodes),
        'can_view_mom': can_view_mom,
        'can_view_baby': can_view_baby,
        'permission_notices': permission_notices,
    }
    context.update(switcher_data)
    return render(request, 'history/v3_baby_growth.html', context)
