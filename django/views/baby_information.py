import datetime
from django.contrib import messages
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from core.models import BabyInformation, FamilyMember
from views import baby_utils
from views.pregnancycase import resolve_active_pregnancy_case, validate_birth_datetime
from views.session_utils import get_current_user_profile
# validate_birth_vitals 已集中定義於 baby_utils，這裡透過 baby_utils.validate_birth_vitals 呼叫


# ==================== 1. 新增功能 ====================
def add_baby_information(request):
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')
    case = resolve_active_pregnancy_case(request, user)
    if not case:
        return redirect('pregnancy_case')

    if case.user_id != user.user_id:
        membership = FamilyMember.objects.filter(pregnancycase=case, user=user).first()
        if not baby_utils.has_permission(membership, 'baby_records', 'edit'):
            return redirect('pregnancy_case')

    if request.method == 'POST':
        gender = (request.POST.get('gender') or '').strip()
        if gender not in {'1', '2'}:
            return render(request, 'baby/add_babyinformation.html', {
                'error': '請選擇性別',
                'case': case,
                'form_data': request.POST
            })

        b_time = None
        if (request.POST.get('birthdaytime') or '').strip():
            try:
                b_time = timezone.make_aware(datetime.datetime.strptime(request.POST.get('birthdaytime').strip(), '%Y-%m-%dT%H:%M'))
            except ValueError:
                return render(request, 'baby/add_babyinformation.html', {
                    'error': '日期時間格式不正確',
                    'case': case,
                    'form_data': request.POST
                })

        # ── 嚴格的出生時間多重防線驗證（共用 pregnancycase.validate_birth_datetime） ──
        birth_error = validate_birth_datetime(case.menstruation, b_time)
        if birth_error:
            return render(request, 'baby/add_babyinformation.html', {
                'error': birth_error,
                'case': case,
                'form_data': request.POST
            })

        # ── 體徵範圍驗證（體重單位 kg，其餘 cm） ──
        w  = baby_utils.parse_float(request.POST.get('birth_weight'))
        h  = baby_utils.parse_float(request.POST.get('birth_height'))
        hc = baby_utils.parse_float(request.POST.get('birth_head'))
        cc = baby_utils.parse_float(request.POST.get('birth_chest'))
        vital_error = baby_utils.validate_birth_vitals(w, h, hc, cc)
        if vital_error:
            return render(request, 'baby/add_babyinformation.html', {
                'error': vital_error,
                'case': case,
                'form_data': request.POST
            })

        # 驗證通過，建立新資料
        new_baby = BabyInformation.objects.create(
            pregnancycase=case,
            name=(request.POST.get('baby_name') or '').strip() or '小寶',
            gender=gender,
            birthdaytime=b_time,
            baby_height=h,
            baby_weight=w,
            babyheadcircumference=hc,
            chestcircumference=cc,
            production_method=(request.POST.get('production_method') or '').strip(),
        )
        request.session['active_baby_id'] = new_baby.baby_id
        request.session.modified = True
        return redirect('babyinformation')

    return render(request, 'baby/add_babyinformation.html', {'case': case})

def delete_baby_information(request):
    """刪除單一寶寶，不影響同一 case 底下的其他寶寶／懷孕紀錄本身。"""
    if request.method != 'POST':
        return redirect('pregnancy_case')

    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    baby_id = request.POST.get('baby_id')
    baby = get_object_or_404(BabyInformation, baby_id=baby_id)
    case = baby.pregnancycase

    if not case or case.user_id != user.user_id:
        return redirect('pregnancy_case')

    if request.session.get('active_baby_id') == baby.baby_id:
        request.session.pop('active_baby_id', None)
        request.session.modified = True

    baby_name = baby.name
    baby.delete()
    messages.success(request, f'已刪除「{baby_name}」的嬰幼兒資訊。')
    return redirect('pregnancy_case')

# ==================== 2. 編輯功能 ====================
def edit_baby_information(request):
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    # 從 URL 參數強制切換 active_baby（供 pregnancycase 頁面的登記出生按鈕使用）
    # 安全性：必須先確認這位寶寶確實屬於目前使用者可存取的個案，
    # 否則任何人都能用 ?baby_id= 把別人的寶寶寫進自己的 session
    #（判斷條件比照 pregnancycase.sync_active_selection_from_request）
    baby_id_param = request.GET.get('baby_id')
    if baby_id_param:
        try:
            baby_obj = BabyInformation.objects.select_related('pregnancycase').filter(
                baby_id=int(baby_id_param)
            ).first()
            if baby_obj and baby_obj.pregnancycase_id and (
                baby_obj.pregnancycase.user_id == user.user_id
                or FamilyMember.objects.filter(
                    pregnancycase=baby_obj.pregnancycase, user=user
                ).exists()
            ):
                request.session['active_baby_id'] = baby_obj.baby_id
                request.session['active_case_id'] = baby_obj.pregnancycase_id
                request.session.modified = True
        except (ValueError, TypeError):
            pass

    active_baby = baby_utils.get_active_baby(request)
    if active_baby is None:
        return redirect('pregnancy_case')

    if active_baby.pregnancycase and active_baby.pregnancycase.user_id != user.user_id:
        membership = FamilyMember.objects.filter(pregnancycase=active_baby.pregnancycase, user=user).first()
        if not baby_utils.has_permission(membership, 'baby_records', 'edit'):
            return redirect('babyinformation')

    if request.method == 'POST':
        # 逐欄位鎖定（有值 = 已填過，不再覆蓋）
        dt_locked = active_baby.birthdaytime is not None
        wt_locked = active_baby.baby_weight is not None
        ht_locked = active_baby.baby_height is not None
        hc_locked = active_baby.babyheadcircumference is not None
        cc_locked = active_baby.chestcircumference is not None
        pm_locked = bool(active_baby.production_method)

        lmp = active_baby.pregnancycase.menstruation if active_baby.pregnancycase else None

        def _err(msg):
            """驗證失敗：用與 GET 相同的完整 context 回到表單，並用 form_data 回填使用者輸入。
            （舊版少傳 locks / join_code / gender_choices，會讓鎖定欄位與性別選單整個消失）"""
            context = _build_edit_context(active_baby)
            context.update({
                'error': msg,
                'form_data': request.POST,
                'birthdaytime_value': request.POST.get('birthdaytime', '') or context['birthdaytime_value'],
                # 鎖定狀態一律用「進入本次 POST 前」的值：
                # 中途已寫進記憶體但尚未 save 的欄位不算已鎖定
                'locks': {
                    'birthdaytime': dt_locked, 'baby_weight': wt_locked,
                    'baby_height': ht_locked, 'babyheadcircumference': hc_locked,
                    'chestcircumference': cc_locked, 'production_method': pm_locked,
                },
            })
            return render(request, 'baby/edit_babyinformation.html', context)

        # 名稱永遠可改
        name = (request.POST.get('baby_name') or '').strip()
        if name:
            active_baby.name = name

        gender = (request.POST.get('gender') or '').strip()
        if gender not in {'1', '2'}:
            return _err('請選擇性別')
        active_baby.gender = gender

        # 出生時間
        if not dt_locked:
            raw_dt = (request.POST.get('birthdaytime') or '').strip()
            if raw_dt:
                try:
                    new_dt = timezone.make_aware(
                        datetime.datetime.strptime(raw_dt, '%Y-%m-%dT%H:%M'))
                except ValueError:
                    return _err('日期時間格式不正確')
                birth_error = validate_birth_datetime(lmp, new_dt)
                if birth_error:
                    return _err(birth_error)
                active_baby.birthdaytime = new_dt

        # 出生體徵（未鎖定的欄位才解析）
        w  = None if wt_locked else baby_utils.parse_float(request.POST.get('birth_weight'))
        h  = None if ht_locked else baby_utils.parse_float(request.POST.get('birth_height'))
        hc = None if hc_locked else baby_utils.parse_float(request.POST.get('birth_head'))
        cc = None if cc_locked else baby_utils.parse_float(request.POST.get('birth_chest'))

        vital_error = baby_utils.validate_birth_vitals(w, h, hc, cc)
        if vital_error:
            return _err(vital_error)

        if w  is not None: active_baby.baby_weight           = w
        if h  is not None: active_baby.baby_height           = h
        if hc is not None: active_baby.babyheadcircumference = hc
        if cc is not None: active_baby.chestcircumference    = cc
        if not pm_locked:
            pm = (request.POST.get('production_method') or '').strip()
            if pm:
                active_baby.production_method = pm

        active_baby.save()
        return redirect('babyinformation')

    # ── GET 請求 ──────────────────────────────────────────────────────
    return render(request, 'baby/edit_babyinformation.html', _build_edit_context(active_baby))


def _build_edit_context(active_baby):
    """編輯嬰幼兒資料頁的完整 context（GET 與所有驗證失敗路徑共用）。"""
    case = active_baby.pregnancycase if active_baby.pregnancycase_id else None

    lmp_date_value = ''
    birth_weeks_value = ''
    due_date_value = ''
    if case and case.menstruation:
        lmp_date_value = case.menstruation.strftime('%Y-%m-%d')
    if case and case.expecteddate:
        due_date_value = case.expecteddate.strftime('%Y-%m-%d')
    if active_baby.birthdaytime and case and case.menstruation:
        birth_weeks_value = baby_utils.get_birth_week(active_baby) or ''

    birthdaytime_value = (
        active_baby.birthdaytime.strftime('%Y-%m-%dT%H:%M') if active_baby.birthdaytime else ''
    )

    return {
        'baby': active_baby,
        'gender_choices': BabyInformation.GENDER_CHOICES,
        'birthdaytime_value': birthdaytime_value,
        'join_code': getattr(case, 'code', '') if case else '',
        'lmp_date_value': lmp_date_value,
        'due_date_value': due_date_value,
        'birth_weeks_value': birth_weeks_value,
        'locks': {
            'birthdaytime':       active_baby.birthdaytime is not None,
            'baby_weight':        active_baby.baby_weight is not None,
            'baby_height':        active_baby.baby_height is not None,
            'babyheadcircumference': active_baby.babyheadcircumference is not None,
            'chestcircumference': active_baby.chestcircumference is not None,
            'production_method':  bool(active_baby.production_method),
        },
    }
