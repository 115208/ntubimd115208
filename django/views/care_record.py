import datetime

from django.shortcuts import render, redirect
from django.utils import timezone
from zoneinfo import ZoneInfo

from core.models import CareStatus, CareRecord, FamilyMember

from views.pregnancycase import url_with_active_selection, resolve_active_pregnancy_case
from views.session_utils import get_current_user_profile
from views import baby_utils

# 內容欄位 CareRecord.content 的 max_length 是 100，截斷長度必須與 schema 一致，
# 否則超過 100 字會被 MySQL 拒絕（strict mode）或無聲截斷。
CONTENT_MAX_LENGTH = 100


def _parse_selected_date(raw):
    try:
        return datetime.date.fromisoformat(raw) if raw else timezone.localdate()
    except Exception:
        return timezone.localdate()

def _check_care_permission(user, case, required='view'):
    """胎數擁有者永遠可讀寫；協助者依 permissions['care_records'] 判斷。"""
    if case.user_id == user.user_id:
        return True
    membership = FamilyMember.objects.filter(pregnancycase=case, user=user).first()
    return baby_utils.has_permission(membership, 'care_records', required,  default='view')


def _care_record_for(case, current_user, care_id):
    """在目前胎數內尋找待辦。"""
    if not care_id:
        return None
    try:
        care_id = int(care_id)
    except (TypeError, ValueError):
        return None
    return CareRecord.objects.filter(
        pregnancycase=case,
        carerecord_id=care_id,
    ).first()


def _redirect_home_with_error(request, selected_date, error_code):
    """權限不足時帶著錯誤碼回首頁，而不是無聲 redirect。"""
    return redirect(url_with_active_selection(request, '/', {
        'date': selected_date.isoformat(),
        'care_error': error_code,
    }))


def add_care_reminder(request):
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    case = resolve_active_pregnancy_case(request, current_user)
    if not case:
        return redirect('pregnancy_case')

    selected_date = _parse_selected_date(request.GET.get('date'))

    if not _check_care_permission(current_user, case, required='edit'):
        return _redirect_home_with_error(request, selected_date, 'care_edit')

    error_message = None
    form_data = {}

    if request.method == 'POST':
        form_data = {
            'record_date': request.POST.get('record_date', ''),
            'record_time': request.POST.get('record_time', ''),
            'carestatus_id': request.POST.get('carestatus_id', ''),
            'content': request.POST.get('content', ''),
        }

        try:
            rd = datetime.date.fromisoformat(form_data['record_date'])
            rt = datetime.time.fromisoformat(form_data['record_time'])
            record_dt = timezone.make_aware(datetime.datetime.combine(rd, rt), ZoneInfo('Asia/Taipei'))
        except Exception:
            error_message = '請提供有效的日期與時間。'

        carestatus = None
        if not error_message:
            carestatus = CareStatus.objects.filter(carestatus_id=form_data['carestatus_id']).first()
            if not carestatus:
                error_message = '請選擇有效的類別。'

        if not error_message:
            # carerecord_id 是 AutoField，主鍵交給資料庫產生；
            # 原本手算 Max+1 在多人同時新增時會撞鍵。
            CareRecord.objects.create(
                pregnancycase=case,
                carestatus=carestatus,
                recordtime=record_dt,
                content=form_data['content'][:CONTENT_MAX_LENGTH],
                create_time=datetime.datetime.now(ZoneInfo('Asia/Taipei')).replace(tzinfo=None),
            )
            return redirect(url_with_active_selection(request, '/', {'date': form_data['record_date']}))

    carestatus_list = list(CareStatus.objects.all())
    return render(request, 'index/care_reminder.html', {
        'selected_date_iso': selected_date.isoformat(),
        'selected_date_label': selected_date.strftime('%Y年%m月%d日'),
        'carestatus_list': carestatus_list,
        'error_message': error_message,
        'form_data': form_data,
    })


def edit_care_reminder(request):
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    case = resolve_active_pregnancy_case(request, current_user)
    if not case:
        return redirect('pregnancy_case')

    selected_date = _parse_selected_date(request.GET.get('date') or request.POST.get('selected_date'))

    if not _check_care_permission(current_user, case, required='edit'):
        return _redirect_home_with_error(request, selected_date, 'care_edit')

    # carerecord_id 可能來自查詢字串 (GET 進入編輯頁) 或表單 (POST 送出更新)
    care_id = request.GET.get('carerecord_id') or request.POST.get('carerecord_id')
    # 用 case 篩選，不限定 user=current_user，讓同一胎數的協助者也能編輯同一份清單
    care_record = _care_record_for(case, current_user, care_id)
    if not care_record:
        return _redirect_home_with_error(request, selected_date, 'care_missing')

    error_message = None

    orig_local_dt = timezone.localtime(care_record.recordtime)

    if request.method == 'POST':
        form_data = {
            'record_date': request.POST.get('record_date', ''),
            'record_time': request.POST.get('record_time', '') or orig_local_dt.time().strftime('%H:%M'),
            'carestatus_id': request.POST.get('carestatus_id', ''),
            'content': request.POST.get('content', ''),
        }

        try:
            rd = datetime.date.fromisoformat(form_data['record_date'])
            rt = datetime.time.fromisoformat(form_data['record_time'])
            record_dt = timezone.make_aware(datetime.datetime.combine(rd, rt), ZoneInfo('Asia/Taipei'))
        except Exception:
            error_message = '請提供有效的日期與時間。'

        carestatus = None
        if not error_message:
            carestatus = CareStatus.objects.filter(carestatus_id=form_data['carestatus_id']).first()
            if not carestatus:
                error_message = '請選擇有效的類別。'

        if not error_message:
            care_record.carestatus = carestatus
            care_record.recordtime = record_dt
            care_record.content = form_data['content'][:CONTENT_MAX_LENGTH]
            care_record.save(update_fields=['carestatus', 'recordtime', 'content'])
            return redirect(url_with_active_selection(request, '/', {'date': form_data['record_date']}))

        carestatus_list = list(CareStatus.objects.all())
        return render(request, 'index/care_reminder.html', {
            'carerecord_id': care_record.carerecord_id,
            'selected_date_iso': selected_date.isoformat(),
            'selected_date_label': selected_date.strftime('%Y年%m月%d日'),
            'carestatus_list': carestatus_list,
            'error_message': error_message,
            'form_data': form_data,
        })

    # GET：帶入既有資料作為表單預設值（確保使用本地時間，避免時區偏差）
    carestatus_list = list(CareStatus.objects.all())
    form_data = {
        'record_date': orig_local_dt.date().isoformat(),
        'record_time': orig_local_dt.time().strftime('%H:%M'),
        'carestatus_id': str(care_record.carestatus_id) if care_record.carestatus_id else '',
        'content': care_record.content or '',
    }
    return render(request, 'index/care_reminder.html', {
        'carerecord_id': care_record.carerecord_id,
        'selected_date_iso': selected_date.isoformat(),
        'selected_date_label': selected_date.strftime('%Y年%m月%d日'),
        'carestatus_list': carestatus_list,
        'error_message': error_message,
        'form_data': form_data,
    })


def set_care_status(request):
    if request.method != 'POST':
        return redirect('/')

    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    selected_date = _parse_selected_date(request.POST.get('selected_date'))
    case = resolve_active_pregnancy_case(request, current_user)

    # 檢視權限可更新完成狀態；新增、編輯內容與刪除仍需 edit 權限。
    if not case or not _check_care_permission(current_user, case, required='view'):
        return _redirect_home_with_error(request, selected_date, 'care_edit')

    care_id = request.POST.get('carerecord_id')
    new_state = request.POST.get('state') in ('1', 'true', 'True', 'on')

    if care_id:
        # 用 case 篩選，不再限定 user=current_user，這樣同一胎數的協助者才能操作同一份清單
        care_record = _care_record_for(case, current_user, care_id)
        if care_record:
            care_record.state = new_state
            care_record.save(update_fields=['state'])

    return redirect(url_with_active_selection(request, '/', {'date': selected_date.isoformat()}))


def delete_care_reminder(request):
    if request.method != 'POST':
        return redirect('/')

    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    selected_date = _parse_selected_date(request.POST.get('selected_date'))
    case = resolve_active_pregnancy_case(request, current_user)

    if not case or not _check_care_permission(current_user, case, required='edit'):
        return _redirect_home_with_error(request, selected_date, 'care_edit')

    care_id = request.POST.get('carerecord_id')
    if care_id:
        # 用 case 篩選，不再限定 user=current_user，這樣同一胎數的協助者才能操作同一份清單
        care_record = _care_record_for(case, current_user, care_id)
        if care_record:
            care_record.delete()

    return redirect(url_with_active_selection(request, '/', {'date': selected_date.isoformat()}))