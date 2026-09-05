import datetime
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

    baby.delete()
    return redirect('pregnancy_case')

# ==================== 2. 編輯功能 ====================
def edit_baby_information(request):
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    # 從 URL 參數強制切換 active_baby（供 pregnancycase 頁面的登記出生按鈕使用）
    baby_id_param = request.GET.get('baby_id')
    if baby_id_param:
        try:
            baby_obj = BabyInformation.objects.filter(baby_id=int(baby_id_param)).first()
            if baby_obj:
                request.session['active_baby_id'] = baby_obj.baby_id
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
        # 名稱永遠可改
        name = (request.POST.get('baby_name') or '').strip()
        if name:
            active_baby.name = name

        # 逐欄位鎖定（有值 = 已填過，不再覆蓋）
        dt_locked = active_baby.birthdaytime is not None
        wt_locked = active_baby.baby_weight is not None
        ht_locked = active_baby.baby_height is not None
        hc_locked = active_baby.babyheadcircumference is not None
        cc_locked = active_baby.chestcircumference is not None
        pm_locked = bool(active_baby.production_method)

        lmp = active_baby.pregnancycase.menstruation if active_baby.pregnancycase else None
        join_code = getattr(active_baby.pregnancycase, 'code', '') if active_baby.pregnancycase_id else ''
        lmp_str = lmp.strftime('%Y-%m-%d') if lmp else ''

        def _err(msg):
            return render(request, 'baby/edit_babyinformation.html', {
                'baby': active_baby,
                'error': msg,
                'birthdaytime_value': request.POST.get('birthdaytime', ''),
                'join_code': join_code,
                'lmp_date_value': lmp_str,
                'birth_weeks_value': '',
                'locks': {
                    'birthdaytime': dt_locked, 'baby_weight': wt_locked,
                    'baby_height': ht_locked, 'babyheadcircumference': hc_locked,
                    'chestcircumference': cc_locked, 'production_method': pm_locked,
                },
            })

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
    lmp_date_value = ''
    birth_weeks_value = ''
    due_date_value = ''
    if active_baby.pregnancycase and active_baby.pregnancycase.menstruation:
        lmp = active_baby.pregnancycase.menstruation
        lmp_date_value = lmp.strftime('%Y-%m-%d')
    if active_baby.pregnancycase and active_baby.pregnancycase.expecteddate:
        due_date_value = active_baby.pregnancycase.expecteddate.strftime('%Y-%m-%d')
    if active_baby.birthdaytime and active_baby.pregnancycase and active_baby.pregnancycase.menstruation:
        birth_weeks_value = baby_utils.get_birth_week(active_baby) or ''

    birthdaytime_value = active_baby.birthdaytime.strftime('%Y-%m-%dT%H:%M') if active_baby.birthdaytime else ''
    join_code = getattr(active_baby.pregnancycase, 'code', '') if active_baby.pregnancycase_id else ''

    return render(request, 'baby/edit_babyinformation.html', {
        'baby': active_baby,
        'birthdaytime_value': birthdaytime_value,
        'join_code': join_code,
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
    })
