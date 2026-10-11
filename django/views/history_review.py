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
    get_case_display_baby,
    get_gestation_parts,
    get_lmp_date,
    is_pregnancy_ongoing,
    resolve_active_baby,
    resolve_active_pregnancy_case,
    sync_active_selection_from_request,
)
from views.session_utils import get_current_user_profile
from views import baby_utils

# ── 檢視權限閘門 ───────────────────────────────────────────────────
# 歷史回顧過去完全沒有權限檢查：養育者把協助者的 mom_records 設為 off 後，
# /pregnancyrecord/ 會擋（見 views/pregnancyrecordadd.py），但協助者只要直接開
# /history-review/ 就看得到孕媽咪的心情日記、體重、血壓與超音波照。
# 這裡統一以 pregnancy_case 為權限基準：個案擁有者永遠有全部權限，
# 其餘一律必須是該個案的 FamilyMember，並依 permissions 過濾內容。
_VIEW_LEVELS = ('view', 'edit')


def _stored_permission(membership, feature):
    """直接讀取 FamilyMember.permissions 中的原始值。

    不用 baby_utils.has_permission 的原因：那裡為了向下相容舊資料，
    把 mom_records 以外的 off 一律視為 view；但歷史回顧會把孕期與寶寶的
    所有細節攤開呈現，這裡必須讓 baby_records 的 off 也真正生效。
    """
    if membership is None:
        return 'off'
    value = (membership.permissions or {}).get(feature, 'view')
    return value if value in ('off',) + _VIEW_LEVELS else 'view'


def resolve_view_permissions(current_user, pregnancy_case):
    """回傳 (可看媽媽相關內容, 可看寶寶相關內容, 提示訊息清單)。"""
    if not pregnancy_case:
        # 沒有任何個案時只查得到自己的資料，維持原本行為
        return True, True, []

    if pregnancy_case.user_id == current_user.user_id:
        return True, True, []

    membership = FamilyMember.objects.filter(
        pregnancycase=pregnancy_case, user=current_user
    ).first()

    can_view_mom = _stored_permission(membership, 'mom_records') in _VIEW_LEVELS
    can_view_baby = _stored_permission(membership, 'baby_records') in _VIEW_LEVELS

    notices = []
    if not can_view_mom:
        notices.append('養育者未開放孕期紀錄的檢視權限，本頁已隱藏產檢、心情、體重與超音波等媽媽相關內容。')
    if not can_view_baby:
        notices.append('養育者未開放嬰幼兒紀錄的檢視權限，本頁已隱藏寶寶相關內容。')
    return can_view_mom, can_view_baby, notices


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


def _calc_stats(current_user, pregnancy_case, active_baby, today,
                can_view_mom=True, can_view_baby=True):
    """計算陪伴天數、照片總數、紀錄總筆數、AI問答次數與里程碑數 (純真實 ORM 數據)。

    沒有檢視權限的類別一律計為 0，避免協助者從統計數字反推被隱藏的內容筆數。
    """
    days_accompanied = 0
    if pregnancy_case:
        lmp = get_lmp_date(pregnancy_case)
        if lmp:
            delta = today - lmp
            days_accompanied = max(0, delta.days)
    elif active_baby and active_baby.birthdaytime:
        birth_date = (
            active_baby.birthdaytime.date()
            if hasattr(active_baby.birthdaytime, 'date')
            else active_baby.birthdaytime
        )
        if birth_date:
            delta = today - birth_date
            days_accompanied = max(0, delta.days)
    else:
        first_preg = (
            PregnancyRecord.objects.filter(user=current_user)
            .order_by('check_date')
            .first()
        )
        if first_preg and first_preg.check_date:
            days_accompanied = max(0, (today - first_preg.check_date).days)

    target_uid = pregnancy_case.user_id if pregnancy_case else current_user.user_id

    if pregnancy_case:
        total_ultrasounds = Prenatalrecord.objects.filter(
            pregnancyrecord__user_id=target_uid, photo__isnull=False
        ).exclude(photo='').count()
        total_baby_photos = BabyRecord.objects.filter(
            baby__pregnancycase=pregnancy_case, photo__isnull=False
        ).exclude(photo='').count()
        total_preg_records = PregnancyRecord.objects.filter(user_id=target_uid).count()
        total_baby_records = BabyRecord.objects.filter(
            baby__pregnancycase=pregnancy_case
        ).count()
        total_milestones = BabyStatus.objects.filter(
            babyrecord__baby__pregnancycase=pregnancy_case
        ).count()
        total_tasks = CareRecord.objects.filter(
            pregnancycase=pregnancy_case, state=True
        ).count()
    else:
        total_ultrasounds = Prenatalrecord.objects.filter(
            pregnancyrecord__user=current_user, photo__isnull=False
        ).exclude(photo='').count()
        total_baby_photos = BabyRecord.objects.filter(
            baby__pregnancycase__user=current_user, photo__isnull=False
        ).exclude(photo='').count()
        total_preg_records = PregnancyRecord.objects.filter(user=current_user).count()
        total_baby_records = BabyRecord.objects.filter(
            baby__pregnancycase__user=current_user
        ).count()
        total_milestones = BabyStatus.objects.filter(
            babyrecord__baby__pregnancycase__user=current_user
        ).count()
        total_tasks = CareRecord.objects.filter(
            pregnancycase__user=current_user, state=True
        ).count()

    # ── 依檢視權限歸零 ──
    if not can_view_mom:
        total_ultrasounds = 0
        total_preg_records = 0
    if not can_view_baby:
        total_baby_photos = 0
        total_baby_records = 0
        total_milestones = 0

    total_photos = total_ultrasounds + total_baby_photos
    total_records = total_preg_records + total_baby_records

    total_qas = QAMessage.objects.filter(
        qa_conversation__user_id=current_user, role='assistant'
    ).count()

    return {
        'days_accompanied': days_accompanied,
        'total_ultrasounds': total_ultrasounds,
        'total_baby_photos': total_baby_photos,
        'total_photos': total_photos,
        'total_records': total_records,
        'total_milestones': total_milestones,
        'total_qas': total_qas,
        'total_tasks': total_tasks,
    }


def history_review(request):
    """主歷史回顧進入點：預設渲染 Page 1 時光軸 (Pregnancy Journey Timeline)"""
    return pregnancy_journey_view(request)


def pregnancy_journey_view(request):
    """
    Page 1: 每日成長軌跡 / 時光軸 (/history-review/ & /history-review/pregnancy-journey/)
    """
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    sync_active_selection_from_request(request, current_user)
    has_baby_selection = bool(
        request.session.get('active_baby_id') or request.GET.get('baby_id')
    )
    active_baby = resolve_active_baby(
        request, current_user, fallback=has_baby_selection
    )
    pregnancy_case = resolve_active_pregnancy_case(request, current_user)
    if not active_baby and pregnancy_case:
        active_baby = get_case_display_baby(pregnancy_case)
    today = timezone.localdate()

    can_view_mom, can_view_baby, permission_notices = resolve_view_permissions(
        current_user, pregnancy_case
    )

    stats = _calc_stats(
        current_user, pregnancy_case, active_baby, today, can_view_mom, can_view_baby
    )
    lmp = get_lmp_date(pregnancy_case) if pregnancy_case else None

    # 1. 成長階段導覽
    stages = [
        {'name': '孕早期', 'icon': '🌱', 'is_active': False},
        {'name': '孕中期', 'icon': '🌸', 'is_active': True},
        {'name': '孕晚期', 'icon': '🤰', 'is_active': False},
        {'name': '出生', 'icon': '👶', 'is_active': False},
        {'name': '滿月', 'icon': '🍼', 'is_active': False},
        {'name': '周歲', 'icon': '🎂', 'is_active': False},
    ]

    # 2. 建構混合 Vertical Timeline Items
    timeline_items = []

    # 2.1 產檢與孕期紀錄 (PregnancyRecord & Prenatalrecord)
    # 權限：mom_records 未開放時完全不查詢，時光軸只留下寶寶相關項目
    target_uid = pregnancy_case.user_id if pregnancy_case else current_user.user_id
    preg_records_qs = (
        PregnancyRecord.objects.filter(user_id=target_uid).order_by('-check_date')
        if can_view_mom
        else PregnancyRecord.objects.none()
    )

    for rec in preg_records_qs:
        prenatal = Prenatalrecord.objects.filter(pregnancyrecord=rec).first()
        feelings_qs = Userfeeling.objects.filter(pregnancyrecord=rec).select_related('feeling')
        feelings = [
            {
                'name': uf.feeling.feeling_name if uf.feeling else '',
                'emoji': FEELING_EMOJI_MAP.get(uf.feeling.feeling_name if uf.feeling else '', '🌸')
            }
            for uf in feelings_qs if uf.feeling
        ]

        weeks = None
        if lmp and rec.check_date:
            delta = rec.check_date - lmp
            if delta.days >= 0:
                weeks = delta.days // 7 + 1

        c_date = rec.check_date or today
        w_str = WEEKDAY_MAP.get(c_date.weekday(), '週日')
        date_display = c_date.strftime('%Y/%m/%d') if hasattr(c_date, 'strftime') else str(c_date)

        if prenatal:
            metrics = []
            if prenatal.sbp and prenatal.dbp:
                metrics.append({'label': '血壓', 'val': f'{prenatal.sbp}/{prenatal.dbp} mmHg'})
            if rec.weight:
                metrics.append({'label': '體重', 'val': f'{rec.weight} kg'})
            f_rate = getattr(prenatal, 'fetal_heart_rate', None) or getattr(prenatal, 'fetus_heart_rate', None)
            if f_rate:
                metrics.append({'label': '胎心率', 'val': f'{f_rate} bpm'})
            if getattr(prenatal, 'edema', None) and prenatal.edema != '-':
                metrics.append({'label': '水腫', 'val': prenatal.edema})

            timeline_items.append({
                'full_date': c_date,
                'date_str': date_display,
                'weekday_str': w_str,
                'type': 'prenatal',
                'icon': '🩺',
                'bg_color': 'bg-[#f4ebff]',
                'text_color': 'text-[#8064A2]',
                'title': f'🩺 產檢紀錄' + (f' (第 {weeks} 週)' if weeks else ''),
                'subtitle': f'孕期第 {weeks} 週' if weeks else '產檢紀錄',
                'content': rec.record or '產檢指標紀錄正常，寶寶持續穩定成長中。',
                'photo': prenatal.photo if prenatal.photo else None,
                'metrics': metrics,
                'feelings': feelings,
                'partner_name': None,
                'partner_avatar': None,
            })
        elif rec.record or feelings or rec.weight:
            timeline_items.append({
                'full_date': c_date,
                'date_str': date_display,
                'weekday_str': w_str,
                'type': 'pregnancy',
                'icon': '❤️',
                'bg_color': 'bg-[#fce4ec]',
                'text_color': 'text-[#d81b60]',
                'title': f'❤️ 今日心情' + (f' (第 {weeks} 週)' if weeks else ''),
                'subtitle': f'孕期第 {weeks} 週筆記' if weeks else '孕期日常',
                'content': rec.record or '紀錄了今天的溫馨心境與身體感受。',
                'photo': None,
                'metrics': [{'label': '體重', 'val': f'{rec.weight} kg'}] if rec.weight else [],
                'feelings': feelings,
                'partner_name': None,
                'partner_avatar': None,
            })

    # 2.2 寶寶紀錄 (BabyRecord)
    if active_baby and can_view_baby:
        baby_records = BabyRecord.objects.filter(baby=active_baby).order_by('-date')
        for br in baby_records:
            statuses = BabyStatus.objects.filter(babyrecord=br).select_related('babygrowthmap')
            milestones = [st.babygrowthmap.growthrecord for st in statuses if st.babygrowthmap]
            b_date = br.date or today
            w_str = WEEKDAY_MAP.get(b_date.weekday(), '週日')
            date_display = b_date.strftime('%Y/%m/%d') if hasattr(b_date, 'strftime') else str(b_date)

            metrics = []
            if br.weight:
                metrics.append({'label': '體重', 'val': f'{br.weight} kg'})
            if br.height:
                metrics.append({'label': '身高', 'val': f'{br.height} cm'})

            title_str = f'👶 寶寶出生 / 成長' if '出生' in str(milestones) else f'👶 {active_baby.name} 成長紀錄'

            timeline_items.append({
                'full_date': b_date,
                'date_str': date_display,
                'weekday_str': w_str,
                'type': 'baby',
                'icon': '👶',
                'bg_color': 'bg-[#fffde7]',
                'text_color': 'text-[#f57f17]',
                'title': title_str,
                'subtitle': f'{active_baby.name} 成長動態',
                'content': br.record or ('紀錄了孩子的成長數據與可愛時刻。' if not milestones else f'成功達成里程碑：{", ".join(milestones)}'),
                'photo': br.photo if br.photo else None,
                'metrics': metrics,
                'feelings': [],
                'partner_name': None,
                'partner_avatar': None,
            })

    # 2.3 照護待辦 (CareRecord)
    # 權限判斷與首頁 views/index.py 完全一致（care_records 的 off 仍向下相容視為 view）
    can_view_care = True
    if pregnancy_case and pregnancy_case.user_id != current_user.user_id:
        care_membership = FamilyMember.objects.filter(
            pregnancycase=pregnancy_case, user=current_user
        ).first()
        can_view_care = baby_utils.has_permission(care_membership, 'care_records', 'view')

    if not can_view_care:
        care_records = CareRecord.objects.none()
    elif pregnancy_case:
        care_records = CareRecord.objects.filter(pregnancycase=pregnancy_case, state=True).order_by('-recordtime')[:15]
    else:
        care_records = CareRecord.objects.filter(
            pregnancycase__user=current_user, state=True
        ).order_by('-recordtime')[:15]
    for cr in care_records:
        r_date = cr.recordtime.date() if hasattr(cr.recordtime, 'date') else today
        w_str = WEEKDAY_MAP.get(r_date.weekday(), '週日')
        date_display = r_date.strftime('%Y/%m/%d') if hasattr(r_date, 'strftime') else str(r_date)
        status_name = cr.carestatus.carestatus if cr.carestatus else '日常照護'

        timeline_items.append({
            'full_date': r_date,
            'date_str': date_display,
            'weekday_str': w_str,
            'type': 'task',
            'icon': '✓',
            'bg_color': 'bg-[#e8f5e9]',
            'text_color': 'text-[#2e7d32]',
            'title': f'✓ 待辦完成：{status_name}',
            'subtitle': '照護任務已完成',
            'content': cr.content or f'完成了「{status_name}」健康照護事項。',
            'photo': None,
            'metrics': [],
            'feelings': [],
            'partner_name': '爸爸',
            'partner_avatar': '👨',
        })

    # 排序：用真正的日期欄位，不要用格式化後的字串
    timeline_items.sort(key=lambda x: x['full_date'] or today, reverse=True)

    context = {
        'current_user': current_user,
        'pregnancy_case': pregnancy_case,
        'active_baby': active_baby,
        'active_tab': 'timeline',
        'stages': stages,
        'timeline_items': timeline_items,
        'has_timeline': bool(timeline_items),
        'stats': stats,
        'today_date_str': today.strftime('%Y/%m/%d'),
        'can_view_mom': can_view_mom,
        'can_view_baby': can_view_baby,
        'permission_notices': permission_notices,
    }
    return render(request, 'history/pregnancy_journey.html', context)


def memory_wall_view(request):
    """
    Page 2: 回顧牆 / 相簿牆 (/history-review/memory-wall/)
    """
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    sync_active_selection_from_request(request, current_user)
    has_baby_selection = bool(
        request.session.get('active_baby_id') or request.GET.get('baby_id')
    )
    active_baby = resolve_active_baby(
        request, current_user, fallback=has_baby_selection
    )
    pregnancy_case = resolve_active_pregnancy_case(request, current_user)
    if not active_baby and pregnancy_case:
        active_baby = get_case_display_baby(pregnancy_case)
    today = timezone.localdate()

    can_view_mom, can_view_baby, permission_notices = resolve_view_permissions(
        current_user, pregnancy_case
    )

    stats = _calc_stats(
        current_user, pregnancy_case, active_baby, today, can_view_mom, can_view_baby
    )
    target_uid = pregnancy_case.user_id if pregnancy_case else current_user.user_id

    # 1. ✨ 那年那天 (Flashback)
    flashback = None
    on_this_day_preg = None
    if can_view_mom:
        on_this_day_preg = PregnancyRecord.objects.filter(
            user_id=target_uid,
            check_date__month=today.month,
            check_date__day=today.day
        ).exclude(check_date=today).first()

    if on_this_day_preg:
        diff_years = max(1, today.year - on_this_day_preg.check_date.year)
        prenatal_p = Prenatalrecord.objects.filter(pregnancyrecord=on_this_day_preg).first()
        flashback = {
            'time_label': f'{diff_years} 年前的今天',
            'date_str': on_this_day_preg.check_date.strftime('%Y/%m/%d'),
            'note': on_this_day_preg.record or '在產檢室第一次聽到寶寶心跳聲 ❤️',
            'photo': prenatal_p.photo if (prenatal_p and prenatal_p.photo) else None,
            'title': '那年的今天 ✨',
        }
    elif can_view_baby:
        if active_baby:
            baby_scope = BabyRecord.objects.filter(baby=active_baby)
        elif pregnancy_case:
            baby_scope = BabyRecord.objects.filter(baby__pregnancycase=pregnancy_case)
        else:
            baby_scope = BabyRecord.objects.filter(baby__pregnancycase__user=current_user)

        on_this_day_baby = baby_scope.filter(
            date__month=today.month, date__day=today.day
        ).exclude(date=today).first()

        if on_this_day_baby:
            diff_years = max(1, today.year - on_this_day_baby.date.year)
            status = BabyStatus.objects.filter(babyrecord=on_this_day_baby).select_related('babygrowthmap').first()
            milestone_name = status.babygrowthmap.growthrecord if status else '成長紀錄'
            flashback = {
                'time_label': f'{diff_years} 年前的今天',
                'date_str': on_this_day_baby.date.strftime('%Y/%m/%d'),
                'note': on_this_day_baby.record or f'紀錄了寶寶【{milestone_name}】的珍貴時刻。',
                'photo': on_this_day_baby.photo if on_this_day_baby.photo else None,
                'title': milestone_name,
            }

    # 2. 按月份分組相簿
    raw_photos = []
    ultrasound_records = (
        Prenatalrecord.objects.filter(pregnancyrecord__user_id=target_uid, photo__isnull=False)
        .exclude(photo='')
        .select_related('pregnancyrecord')
        .order_by('-pregnancyrecord__check_date')
        if can_view_mom
        else Prenatalrecord.objects.none()
    )
    for p in ultrasound_records:
        dt = p.pregnancyrecord.check_date if (p.pregnancyrecord and p.pregnancyrecord.check_date) else today
        raw_photos.append({
            'url': p.photo,
            'date_str': dt.strftime('%m/%d') if hasattr(dt, 'strftime') else str(dt),
            'full_date_str': dt.strftime('%Y/%m/%d') if hasattr(dt, 'strftime') else str(dt),
            'year_month': dt.strftime('%Y 年 %m 月') if hasattr(dt, 'strftime') else '歷史相簿',
            'title': '產檢超音波照',
            'stage': '孕期紀錄',
            'sort_date': dt if hasattr(dt, 'strftime') else today,
        })

    if not can_view_baby:
        baby_photo_records = BabyRecord.objects.none()
    else:
        if active_baby:
            baby_photo_qs = BabyRecord.objects.filter(baby=active_baby)
        elif pregnancy_case:
            baby_photo_qs = BabyRecord.objects.filter(baby__pregnancycase=pregnancy_case)
        else:
            baby_photo_qs = BabyRecord.objects.filter(baby__pregnancycase__user=current_user)
        baby_photo_records = (
            baby_photo_qs.filter(photo__isnull=False)
            .exclude(photo='')
            .select_related('baby')
            .order_by('-date')
        )
    for br in baby_photo_records:
        dt = br.date or today
        baby_name = br.baby.name if br.baby else '寶寶'
        status = BabyStatus.objects.filter(babyrecord=br).select_related('babygrowthmap').first()
        title = status.babygrowthmap.growthrecord if status else f'{baby_name} 成長相片'

        stage_label = '寶寶成長紀錄'
        if br.baby and br.baby.birthdaytime and br.date:
            b_date = br.baby.birthdaytime.date() if hasattr(br.baby.birthdaytime, 'date') else br.baby.birthdaytime
            months = max(0, (br.date.year - b_date.year) * 12 + br.date.month - b_date.month)
            stage_label = f'寶寶 {months} 個月'

        raw_photos.append({
            'url': br.photo,
            'date_str': dt.strftime('%m/%d') if hasattr(dt, 'strftime') else str(dt),
            'full_date_str': dt.strftime('%Y/%m/%d') if hasattr(dt, 'strftime') else str(dt),
            'year_month': dt.strftime('%Y 年 %m 月') if hasattr(dt, 'strftime') else '歷史相簿',
            'title': title,
            'stage': stage_label,
            'sort_date': dt if hasattr(dt, 'strftime') else today,
        })

    # 排序：用真正的日期欄位，不要用格式化後的字串
    raw_photos.sort(key=lambda x: x['sort_date'], reverse=True)

    grouped_months_dict = {}
    for item in raw_photos:
        ym = item['year_month']
        if ym not in grouped_months_dict:
            grouped_months_dict[ym] = {
                'year_month': ym,
                'stage_label': item['stage'],
                'photos': []
            }
        grouped_months_dict[ym]['photos'].append(item)

    memory_wall_groups = list(grouped_months_dict.values())

    context = {
        'current_user': current_user,
        'pregnancy_case': pregnancy_case,
        'active_baby': active_baby,
        'active_tab': 'photos',
        'stats': stats,
        'flashback': flashback,
        'has_flashback': bool(flashback),
        'memory_wall_groups': memory_wall_groups,
        'has_memory_wall': bool(memory_wall_groups),
        'today_date_str': today.strftime('%Y/%m/%d'),
        'can_view_mom': can_view_mom,
        'can_view_baby': can_view_baby,
        'permission_notices': permission_notices,
    }
    return render(request, 'history/memory_wall.html', context)


def baby_growth_view(request):
    """
    Page 3: 階段總結 / 紀念冊 (/history-review/baby-growth/ & /history-review/phase-review/)
    支援 ?mode=pregnancy (孕期畢業報告) 與 ?mode=baby (寶寶 1 歲精華) 雙模式切換！
    """
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    sync_active_selection_from_request(request, current_user)
    has_baby_selection = bool(
        request.session.get('active_baby_id') or request.GET.get('baby_id')
    )
    active_baby = resolve_active_baby(
        request, current_user, fallback=has_baby_selection
    )
    pregnancy_case = resolve_active_pregnancy_case(request, current_user)
    if not active_baby and pregnancy_case:
        active_baby = get_case_display_baby(pregnancy_case)
    today = timezone.localdate()

    can_view_mom, can_view_baby, permission_notices = resolve_view_permissions(
        current_user, pregnancy_case
    )

    stats = _calc_stats(
        current_user, pregnancy_case, active_baby, today, can_view_mom, can_view_baby
    )
    lmp = get_lmp_date(pregnancy_case) if pregnancy_case else None

    # 切換模式：預設若有 baby 則顯示 baby 模式，亦可透過 ?mode= 切換
    mode = request.GET.get('mode', 'baby' if active_baby else 'pregnancy')
    # 沒有檢視權限的模式直接切到另一邊，避免整頁空白又不知原因
    if mode == 'baby' and not can_view_baby and can_view_mom:
        mode = 'pregnancy'
    elif mode == 'pregnancy' and not can_view_mom and can_view_baby:
        mode = 'baby'

    if mode == 'baby':
        baby_name = active_baby.name if active_baby else '寶寶'
        recap_title = f'{baby_name} 成長精華 🎓'
        recap_subtitle = '溫暖陪伴，見證小寶每一個珍貴的第一次！'
        hero_blessing = '🍼 「從發出第一個聲音，到跨出第一步，謝謝你平安健康長大。」'
        weight_title = '📈 寶寶成長曲線'
        mom_letter_title = '給爸媽的一段話'
    else:
        recap_title = '孕期畢業典禮 🎓'
        recap_subtitle = '40 週的陪伴，謝謝你的勇敢與堅強！'
        hero_blessing = '🌸 「感謝這 280 個日夜的堅持，每一刻的付出都凝聚成生命的奇蹟。」'
        weight_title = '📈 孕期體重變化'
        mom_letter_title = '給媽媽的一段話'

    # ── 重要里程碑：一律取自真實資料 ──────────────────────────────
    # 這是健康類系統，絕對不可把預設清單當成使用者真的達成過的紀錄。
    # 寶寶模式：BabyStatus × BabyGrowthMap 的實際達成紀錄。
    # 孕期模式：目前資料模型沒有「孕期里程碑」這種資料表，
    #           因此一律回空清單，由模板顯示「資料不足」空狀態。
    milestones = []
    if mode == 'baby' and active_baby and can_view_baby:
        achieved = (
            BabyStatus.objects.filter(babyrecord__baby=active_baby)
            .select_related('babygrowthmap', 'babyrecord')
            .order_by('babygrowthmap__timecourse')
        )
        for st in achieved:
            if not st.babygrowthmap:
                continue
            achieved_date = st.babyrecord.date if st.babyrecord else None
            milestones.append({
                'title': st.babygrowthmap.growthrecord,
                'week': f'{st.babygrowthmap.timecourse} 個月',
                'icon': '⭐',
                'achieved_date': achieved_date.strftime('%Y/%m/%d') if achieved_date else '',
            })

    # ── 體重趨勢點（取自 ORM，均勻採樣 5 個點配合 SVG 版面）──────
    weight_points = []
    weight_unavailable_reason = ''
    if mode == 'baby':
        if not can_view_baby:
            weight_unavailable_reason = '養育者未開放嬰幼兒紀錄的檢視權限。'
        elif not active_baby:
            weight_unavailable_reason = '尚未選擇寶寶。'
        else:
            b_records = list(
                BabyRecord.objects.filter(baby=active_baby, weight__isnull=False).order_by('date')
            )
            b_birth = (
                active_baby.birthdaytime.date()
                if hasattr(active_baby.birthdaytime, 'date')
                else active_baby.birthdaytime
            )
            for br in _sample_evenly(b_records, 5):
                if b_birth and br.date:
                    months = (br.date.year - b_birth.year) * 12 + br.date.month - b_birth.month
                    if br.date.day < b_birth.day:
                        months -= 1
                    w_label = f'{max(0, months)}個月' if months > 0 else '出生'
                else:
                    w_label = br.date.strftime('%m/%d') if br.date else '紀錄'
                weight_points.append({'week': w_label, 'val': float(br.weight)})
    else:
        if not can_view_mom:
            weight_unavailable_reason = '養育者未開放孕期紀錄的檢視權限。'
        else:
            target_uid = pregnancy_case.user_id if pregnancy_case else current_user.user_id
            weight_records = list(
                PregnancyRecord.objects.filter(user_id=target_uid, weight__isnull=False).order_by('check_date')
            )
            for wr in _sample_evenly(weight_records, 5):
                w_label = wr.check_date.strftime('%m/%d') if wr.check_date else '紀錄'
                if lmp and wr.check_date:
                    d = (wr.check_date - lmp).days
                    if d >= 0:
                        w_label = f'{d // 7 + 1}週'
                weight_points.append({'week': w_label, 'val': float(wr.weight)})

    # 折線圖座標一律在後端算好，模板不再使用任何寫死的 SVG path
    weight_chart = _build_weight_chart(weight_points)
    if not weight_chart['has_data'] and not weight_unavailable_reason:
        weight_unavailable_reason = '體重紀錄少於 2 筆，資料不足以繪製趨勢圖。'

    # ── 心情統計（取自 ORM；沒有資料就顯示空狀態，不再塞假比例）──
    target_uid = pregnancy_case.user_id if pregnancy_case else current_user.user_id
    feeling_counts = []
    if can_view_mom:
        feeling_counts = list(
            Userfeeling.objects.filter(
                pregnancyrecord__user_id=target_uid
            ).values('feeling__feeling_name').annotate(cnt=Count('feeling')).order_by('-cnt')
        )

    mood_distribution = _build_mood_distribution(feeling_counts)

    # ── 手寫筆記（取自 ORM；沒有就留白，不虛構媽媽的話）──────────
    latest_note_rec = None
    if can_view_mom:
        latest_note_rec = PregnancyRecord.objects.filter(
            user_id=target_uid, record__isnull=False
        ).exclude(record='').order_by('-check_date').first()
    mom_letter = latest_note_rec.record if latest_note_rec else ''

    context = {
        'current_user': current_user,
        'pregnancy_case': pregnancy_case,
        'active_baby': active_baby,
        'active_tab': 'recap',
        'mode': mode,
        'stats': stats,
        'milestones': milestones,
        'has_milestones': bool(milestones),
        'weight_points': weight_chart['points'],
        'weight_polyline': weight_chart['polyline'],
        'has_weight_chart': weight_chart['has_data'],
        'weight_unavailable_reason': weight_unavailable_reason,
        'mood_distribution': mood_distribution,
        'has_mood': bool(mood_distribution),
        'mom_letter': mom_letter,
        'recap_title': recap_title,
        'recap_subtitle': recap_subtitle,
        'hero_blessing': hero_blessing,
        'weight_title': weight_title,
        'mom_letter_title': mom_letter_title,
        'today_date_str': today.strftime('%Y/%m/%d'),
        'current_year': today.year,
        'can_view_mom': can_view_mom,
        'can_view_baby': can_view_baby,
        'permission_notices': permission_notices,
    }
    return render(request, 'history/baby_growth.html', context)


# ── 圖表資料輔助函式（座標一律在後端算好再傳給模板）────────────────
def _sample_evenly(items, count):
    """從清單中均勻採樣最多 count 筆（保留原順序、不重複）。"""
    total = len(items)
    if total <= count:
        return list(items)
    picked = []
    seen = set()
    for i in range(count):
        idx = round(i * (total - 1) / (count - 1))
        if idx not in seen:
            seen.add(idx)
            picked.append(items[idx])
    return picked


def _build_weight_chart(points, x_start=10, x_end=190, y_top=15, y_bottom=70):
    """把真實體重數值換算成 SVG 座標（viewBox 0 0 200 80）。

    少於 2 筆一律回 has_data=False，由模板顯示「資料不足」空狀態；
    絕對不補任何虛構數值。
    """
    if len(points) < 2:
        return {'points': [], 'polyline': '', 'has_data': False}

    values = [p['val'] for p in points]
    v_min, v_max = min(values), max(values)
    span = v_max - v_min
    n = len(points)

    plotted = []
    for i, p in enumerate(points):
        x = x_start + (x_end - x_start) * i / (n - 1)
        ratio = 0.5 if span == 0 else (p['val'] - v_min) / span
        y = y_bottom - ratio * (y_bottom - y_top)
        plotted.append({
            'week': p['week'],
            'val': p['val'],
            'x': f'{x:.1f}',
            'y': f'{y:.1f}',
        })

    polyline = ' '.join(f"{p['x']},{p['y']}" for p in plotted)
    return {'points': plotted, 'polyline': polyline, 'has_data': True}


def _build_mood_distribution(feeling_counts, colors=None):
    """把心情統計換算成甜甜圈圖需要的百分比與 dash 參數。

    沒有任何心情紀錄時回空清單（模板顯示空狀態），不再塞入假的心情比例。
    """
    colors = colors or ['#8064A2', '#b2e4fb', '#f8bbd0', '#e3e3df']
    total = sum(item['cnt'] for item in feeling_counts)
    if total <= 0:
        return []

    distribution = []
    offset = 0
    for idx, item in enumerate(feeling_counts[:4]):
        pct = round((item['cnt'] / total) * 100)
        distribution.append({
            'name': item['feeling__feeling_name'] or '未知',
            'pct': pct,
            'count': item['cnt'],
            'color': colors[idx % len(colors)],
            # stroke-dasharray 以 100 為總周長，offset 為負的累計百分比
            'dash_array': f'{pct}, 100',
            'dash_offset': -offset,
        })
        offset += pct
    return distribution


def ai_growth_journey_view(request):
    """【階段/年度總結】別名路由"""
    return baby_growth_view(request)
