import datetime
from django.db.models import Q
from django.http import HttpResponseNotAllowed
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from views import baby_utils
from views.upload_utils import InvalidImageError
from views.supabase_storage import upload_image, delete_image

MONTH_ABBR = ['', 'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']

# ── 成長紀錄體徵合理範圍（0~3歲，體重 kg，其餘 cm）──
# 身高下限 25cm：對齊出生時的下限，確保極早產兒（22~24週）出生後第一筆紀錄不被擋住
RECORD_VITAL_RANGES = {
    'height':             (25.0,  130.0, '身高（cm）合理範圍為 25 ~ 130 cm'),
    'weight':             (0.3,   30.0,  '體重（kg）合理範圍為 0.3 ~ 30 kg'),
    'headcircumference':  (25.0,  60.0,  '頭圍（cm）合理範圍為 25 ~ 60 cm'),
    'chestcircumference': (20.0,  60.0,  '胸圍（cm）合理範圍為 20 ~ 60 cm'),
}

def _validate_record_vitals(height, weight, head, chest):
    """驗證成長紀錄體徵數值是否在合理範圍內。回傳 None 代表合法；否則回傳錯誤訊息。"""
    pairs = [
        (height, 'height'),
        (weight, 'weight'),
        (head,   'headcircumference'),
        (chest,  'chestcircumference'),
    ]
    for value, key in pairs:
        if value is None:
            continue
        lo, hi, msg = RECORD_VITAL_RANGES[key]
        if not (lo <= value <= hi):
            return msg
    return None

from core.models import (
    BabyInformation,
    BabyRecord,
    BabyGrowthMap,
    BabyStatus,
    PregnancyCase,
    FamilyMember,
)

from views.pregnancycase import url_with_active_selection
from views.session_utils import get_current_user_profile


# ── 權限與角色輔助函式 ──────────────────────────────────────────────
def _check_baby_permission(user, case, required='view'):
    """個案擁有者永遠通過；其餘一律必須有 FamilyMember 列且權限足夠。
    case 為 None（資料不完整的孤兒寶寶）一律拒絕，避免無主資料被任何人存取。"""
    if not case:
        return False
    if case.user_id == user.user_id:
        return True
    membership = FamilyMember.objects.filter(pregnancycase=case, user=user).first()
    if membership is None:
        return False
    if required not in ('view', 'edit'):
        raise ValueError(f"_check_baby_permission: unknown required level '{required}', must be 'view' or 'edit'")
    return baby_utils.has_permission(membership, 'baby_records', required)


def _get_accessible_babies(user):
    """取得使用者可存取的全部嬰幼兒（自有 + 家庭成員共享且 perm_baby 不為 none）。"""
    cases_own = PregnancyCase.objects.filter(user=user)

    # 共享：只取 perm_baby 為 viewer 或 caregiver 的 FamilyMember
    shared_memberships = FamilyMember.objects.filter(user=user).select_related('pregnancycase')
    shared_case_ids = [m.pregnancycase_id for m in shared_memberships if baby_utils.get_permission(m, 'baby_records') != 'off']
    return BabyInformation.objects.filter(
        Q(pregnancycase__in=cases_own) | Q(pregnancycase_id__in=shared_case_ids)
    ).distinct()


# ── 表單 context 產生器（GET 與所有驗證失敗路徑共用）────────────────
def _get_add_milestones(baby, record_date):
    """新增頁可供勾選的里程碑（依月齡區間，排除已達成的）。"""
    age_in_months    = baby_utils.calculate_age_in_months(baby.birthdaytime, record_date)
    relevant_courses = baby_utils.get_relevant_timecourses(age_in_months)

    achieved_ids = BabyStatus.objects.filter(
        babyrecord__baby=baby
    ).values_list('babygrowthmap_id', flat=True)

    if relevant_courses:
        return BabyGrowthMap.objects.filter(
            timecourse__in=relevant_courses
        ).exclude(babygrowthmap_id__in=achieved_ids).order_by('timecourse')
    return BabyGrowthMap.objects.all().exclude(
        babygrowthmap_id__in=achieved_ids
    ).order_by('timecourse')


def _build_record_form_context(user, baby, record_date, *, record=None):
    """組出成長紀錄表單需要的完整 context。

    GET 與每一條驗證失敗的 render 都走這裡，避免錯誤畫面漏掉
    baby_list / all_milestones / today_iso 等欄位而讓使用者的輸入與選項被清空。
    record 不為 None 代表編輯頁。
    """
    safe_date = record_date or timezone.localdate()
    context = {
        'baby':                baby,
        'baby_list':           _get_accessible_babies(user),
        'today_iso':           timezone.localdate().isoformat(),
        'selected_month_abbr': MONTH_ABBR[safe_date.month],
        'selected_day':        safe_date.day,
    }
    if record is not None:
        context['is_edit']        = True
        context['record']         = record
        context['all_milestones'] = _get_milestones_for_edit(baby, record)
    else:
        context['is_edit']        = False
        context['all_milestones'] = _get_add_milestones(baby, safe_date) if baby else BabyGrowthMap.objects.none()
    return context


def _selected_milestone_list(raw):
    """把表單送出的 pipe-separated 里程碑字串轉成清單（供模板回填勾選狀態）。"""
    return [m.strip() for m in (raw or '').split('|') if m.strip()]


# ── 新增生長紀錄 ─────────────────────────────────────────────────

def add_baby_record(request):
    """新增嬰幼兒生長紀錄。需要 perm_baby=caregiver 才可寫入。

    同一位寶寶同一天只會有一筆紀錄：若當天已有紀錄，本函式會更新既有紀錄（upsert），
    不再重複新增（過去重複新增會讓首頁合併顯示、卻只編輯得到第一筆）。
    """
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    active_baby = baby_utils.get_active_baby(request)
    today = timezone.localdate()

    if active_baby is None:
        # 沒有可用的寶寶：POST 一律退回並說明，不要靜默吞掉使用者輸入
        context = {
            'baby':                None,
            'baby_list':           _get_accessible_babies(user),
            'all_milestones':      BabyGrowthMap.objects.none(),
            'form_data':           request.POST if request.method == 'POST' else {'date': today.isoformat()},
            'milestones':          request.POST.get('milestones', '') if request.method == 'POST' else '',
            'selected_milestone_list': _selected_milestone_list(request.POST.get('milestones')) if request.method == 'POST' else [],
            'today_iso':           today.isoformat(),
            'selected_month_abbr': MONTH_ABBR[today.month],
            'selected_day':        today.day,
        }
        if request.method == 'POST':
            context['error'] = '目前沒有選擇任何寶寶，無法儲存紀錄。請先於上方切換器選擇寶寶，或建立嬰幼兒資訊。'
        return render(request, 'baby/add_babyrecord.html', context)

    case = active_baby.pregnancycase

    # 權限：只有 caregiver 以上才能新增
    if not _check_baby_permission(user, case, required='edit'):
        return redirect('babyinformation')

    initial_date = request.GET.get('date', '')

    def _error(message, record_date):
        """驗證失敗：帶著完整 context 與使用者原本的輸入回到表單。"""
        context = _build_record_form_context(user, active_baby, record_date)
        context.update({
            'error':                   message,
            'form_data':               request.POST,
            'milestones':              request.POST.get('milestones', ''),
            'selected_milestone_list': _selected_milestone_list(request.POST.get('milestones')),
        })
        return render(request, 'baby/add_babyrecord.html', context)

    if request.method == 'POST':
        date_str = request.POST.get('date')

        if not date_str:
            return _error('請填寫紀錄日期', None)

        # 後端二次驗證日期格式與未來日期
        try:
            record_date_post = datetime.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            return _error('日期格式不正確', None)
        #不能記超過今天日期
        if record_date_post > today:
            return _error('無法新增未來日期的紀錄', None)
        #未出生不能紀錄（birthdaytime=None 表示出生日尚未填寫）
        if not active_baby.birthdaytime:
            return _error('尚未填寫出生日期，無法新增成長紀錄', record_date_post)
        if record_date_post < active_baby.birthdaytime.date():
            return _error('尚未出生，無法新增成長紀錄', record_date_post)

        milestones_str = request.POST.get('milestones', '')
        # 內文直接去除前後空白儲存，里程碑摘要由個別的關聯表來呈現即可
        record_text    = (request.POST.get('record', '') or '').strip()

        # ── 體徵範圍驗證 ──
        h  = baby_utils.parse_float(request.POST.get('height'))
        w  = baby_utils.parse_float(request.POST.get('weight'))
        hc = baby_utils.parse_float(request.POST.get('headcircumference'))
        cc = baby_utils.parse_float(request.POST.get('chestcircumference'))
        vital_error = _validate_record_vitals(h, w, hc, cc)
        if vital_error:
            return _error(vital_error, record_date_post)

        # ── 照片：所有驗證都通過後才真正落地，避免驗證失敗留下孤兒檔 ──
        try:
            photo_url = upload_image(
                request.FILES.get('photo'),
                folder=f'baby_records/{active_baby.pk}',
            )
        except InvalidImageError as exc:
            return _error(str(exc), record_date_post)

        # ── upsert：同一位寶寶同一天只保留一筆紀錄 ──
        existing = (
            BabyRecord.objects
            .filter(baby=active_baby, date=record_date_post)
            .order_by('babyrecord_id')
            .first()
        )
        if existing:
            # 只覆寫這次有填的欄位，避免把當天既有資料清成空白
            if h  is not None: existing.height            = h
            if w  is not None: existing.weight            = w
            if hc is not None: existing.headcircumference = hc
            if cc is not None: existing.chestcircumference = cc
            old_photo_url = existing.photo if photo_url else None
            if photo_url:
                existing.photo = photo_url
            old_text = (existing.record or '').strip()
            if record_text and record_text != old_text:
                existing.record = f'{old_text}\n{record_text}' if old_text else record_text
            existing.update_time = timezone.now()
            try:
                existing.save()
            except Exception:
                # DB 儲存失敗：刪除已上傳的新圖避免孤兒圖片，保留舊圖
                delete_image(photo_url)
                return _error('儲存失敗，請稍後再試', record_date_post)
            delete_image(old_photo_url)
            baby_record = existing
            merged = True
        else:
            try:
                baby_record = BabyRecord.objects.create(
                    baby=active_baby,
                    date=record_date_post,
                    record=record_text,
                    weight=w,
                    height=h,
                    headcircumference=hc,
                    chestcircumference=cc,
                    photo=photo_url,
                )
            except Exception:
                # DB 儲存失敗：刪除已上傳的圖片避免孤兒圖片
                delete_image(photo_url)
                return _error('儲存失敗，請稍後再試', record_date_post)
            merged = False

        # 解析 pipe-separated 里程碑字串，逐一建立 BabyStatus 關聯
        milestone_names = [m.strip() for m in milestones_str.split('|') if m.strip()]
        for m_name in milestone_names:
            growth_map = BabyGrowthMap.objects.filter(growthrecord=m_name).first()
            if growth_map:
                BabyStatus.objects.get_or_create(
                    babyrecord=baby_record,
                    babygrowthmap=growth_map,
                )

        # 儲存成功後跳回嬰幼兒資訊頁（合併既有紀錄時帶旗標，讓頁面提示使用者）
        extra = {'date': record_date_post.isoformat()}
        if merged:
            extra['record_merged'] = '1'
        return redirect(url_with_active_selection(request, reverse('babyinformation'), extra))

    # ── GET：準備表單資料 ────────────────────────────────────────
    try:
        record_date = datetime.date.fromisoformat(initial_date) if initial_date else today
    except Exception:
        record_date = today

    if active_baby.birthdaytime and record_date < active_baby.birthdaytime.date():
        record_date = active_baby.birthdaytime.date()

    context = _build_record_form_context(user, active_baby, record_date)
    context.update({
        'form_data':               {'date': record_date.isoformat()},
        'milestones':              '',
        'selected_milestone_list': [],
        # 當天已有紀錄時先告知使用者：儲存會更新既有紀錄而不是新增第二筆
        'has_record_on_date': BabyRecord.objects.filter(
            baby=active_baby, date=record_date
        ).exists(),
    })
    return render(request, 'baby/add_babyrecord.html', context)


# ── 編輯生長紀錄 ─────────────────────────────────────────────────

def edit_baby_record(request, babyrecord_id):

    """
    編輯特定生長紀錄。
    安全防護：水平越權（只有案例擁有者或 perm_baby=caregiver 的家庭成員可修改）。
    里程碑更新策略：先全量刪除再重新建立，確保與表單送出一致。
    """
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    record = get_object_or_404(BabyRecord, babyrecord_id=babyrecord_id)

    baby   = record.baby
    case   = baby.pregnancycase if baby else None

    # 需要 caregiver 權限才能編輯
    if not _check_baby_permission(user, case, required='edit'):
        return redirect('babyinformation')

    record.milestones, record.note_text = baby_utils.split_note_and_milestones(record)
    today = timezone.localdate()

    def _error(message, record_date):
        context = _build_record_form_context(user, baby, record_date or record.date, record=record)
        context.update({
            'error':                   message,
            'form_data':               request.POST,
            'selected_milestones':     request.POST.get('milestones', ''),
            'selected_milestone_list': _selected_milestone_list(request.POST.get('milestones')),
        })
        return render(request, 'baby/edit_babyrecord.html', context)

    if request.method == 'POST':
        date_str = request.POST.get('date')
        if not date_str:
            return _error('請填寫紀錄日期', None)

        milestones_str = request.POST.get('milestones', '')

        # ── 日期後端驗證（與 add 一致） ──
        try:
            record_date_edit = datetime.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            return _error('日期格式不正確', None)
        if record_date_edit > today:
            return _error('無法修改為未來日期的紀錄', None)
        if baby.birthdaytime and record_date_edit < baby.birthdaytime.date():
            return _error('紀錄日期不可早於寶寶出生日', None)

        # ── 體徵範圍驗證 ──
        h  = baby_utils.parse_float(request.POST.get('height'))
        w  = baby_utils.parse_float(request.POST.get('weight'))
        hc = baby_utils.parse_float(request.POST.get('headcircumference'))
        cc = baby_utils.parse_float(request.POST.get('chestcircumference'))
        vital_error = _validate_record_vitals(h, w, hc, cc)
        if vital_error:
            return _error(vital_error, record_date_edit)

        # 照片：驗證全數通過後才存檔，避免驗證失敗留下孤兒檔
        try:
            photo_url = upload_image(
                request.FILES.get('photo'),
                folder=f'baby_records/{baby.pk}',
            )
        except InvalidImageError as exc:
            return _error(str(exc), record_date_edit)

        record.date               = record_date_edit
        record.record             = (request.POST.get('record', '') or '').strip()
        record.weight             = w
        record.height             = h
        record.headcircumference  = hc
        record.chestcircumference = cc

        # 只有新上傳照片才覆蓋舊照片
        old_photo_url = record.photo if photo_url else None
        if photo_url:
            record.photo = photo_url

        record.update_time = timezone.now()
        record.save()
        delete_image(old_photo_url)

        # 全量重建里程碑關聯（先刪除再建立）
        BabyStatus.objects.filter(babyrecord=record).delete()
        for m_name in [m.strip() for m in milestones_str.split('|') if m.strip()]:
            growth_map = BabyGrowthMap.objects.filter(growthrecord=m_name).first()
            if growth_map:
                BabyStatus.objects.create(babyrecord=record, babygrowthmap=growth_map)

        return redirect(url_with_active_selection(request, reverse('babyinformation')))

    # ── GET：準備表單資料 ──────────────────────────────────
    context = _build_record_form_context(user, baby, record.date, record=record)
    context.update({
        'form_data': {
            'date':               record.date.isoformat() if hasattr(record.date, 'isoformat') else record.date,
            'height':             record.height,
            'weight':             record.weight,
            'headcircumference':  record.headcircumference,
            'chestcircumference': record.chestcircumference,
            'record':             record.note_text,
        },
        'selected_milestones':     '|'.join(record.milestones),
        'selected_milestone_list': list(record.milestones),
    })
    return render(request, 'baby/edit_babyrecord.html', context)


def _get_milestones_for_edit(baby, record):
    """取得可供編輯頁顯示的里程碑列表（月齡區間 + 本筆已勾選，排除其他天已達成的）。"""
    record_date   = record.date
    age_in_months = baby_utils.calculate_age_in_months(baby.birthdaytime, record_date)
    relevant      = baby_utils.get_relevant_timecourses(age_in_months)

    achieved_other = BabyStatus.objects.filter(
        babyrecord__baby=baby
    ).exclude(babyrecord=record).values_list('babygrowthmap_id', flat=True)

    record_milestones = getattr(record, 'milestones', None) or []

    if relevant:
        return BabyGrowthMap.objects.filter(
            Q(timecourse__in=relevant) | Q(growthrecord__in=record_milestones)
        ).exclude(babygrowthmap_id__in=achieved_other).distinct().order_by('timecourse')
    return BabyGrowthMap.objects.all().exclude(
        babygrowthmap_id__in=achieved_other
    ).order_by('timecourse')


# ── 刪除生長紀錄 ─────────────────────────────────────────────────

def delete_baby_record(request, babyrecord_id):
    """
    刪除特定生長紀錄。
    安全防護：
      1. 水平越權：需 perm_baby=caregiver 或案例擁有者
      2. 僅接受 POST，防止 GET 意外觸發刪除
    """
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    record = get_object_or_404(BabyRecord, babyrecord_id=babyrecord_id)
    case   = record.baby.pregnancycase if record.baby else None

    if not _check_baby_permission(user, case, required='edit'):
        return redirect('babyinformation')
    photo_url = record.photo
    record.delete()
    delete_image(photo_url)
    return redirect(url_with_active_selection(request, reverse('babyinformation')))
