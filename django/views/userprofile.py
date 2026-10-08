from datetime import timedelta
from types import SimpleNamespace

from django.shortcuts import render, redirect
from django.utils import timezone

from core.models import BabyRecord, FamilyMember, PregnancyCase, PregnancyRecord, UserProfile
from views import join_request
from views.pregnancycase import (
    get_lmp_date,
    is_pregnancy_ongoing,
    resolve_active_baby,
    resolve_active_pregnancy_case,
    sync_active_selection_from_request,
)
from views.session_utils import get_current_user_profile
from views.supabase_storage import delete_image, upload_image
from views.upload_utils import InvalidImageError, validate_image_upload
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError

def _format_number(value):
    if value is None:
        return '-'
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _annotate_family_roles(family_members, case_owner_id=None):
    """依 case.user_id 判斷養育者，其餘為「協助者」。
    直接在每個 FamilyMember 實例上附加 role_label / is_owner_member 屬性供 template 顯示，不寫入資料庫。
    """
    for member in family_members:
        is_owner = (case_owner_id is not None and member.user_id == case_owner_id)
        member.is_owner_member = is_owner
        member.role_label = '養育者' if is_owner else '協助者'
    return family_members


def _latest_weight_for_selection(request, user):
    record = (
        PregnancyRecord.objects.filter(user=user)
        .exclude(weight__isnull=True)
        .order_by('-check_date', '-pregnancyrecord_id')
        .first()
    )
    if record and record.weight is not None:
        return record.weight
    return '-'


def _build_selected_child_info(request, current_user):
    sync_active_selection_from_request(request, current_user)
    today = timezone.localdate()
    case = resolve_active_pregnancy_case(request, current_user)

    # 修正：先判斷 case 層級狀態。只要這個 case 底下還有寶寶沒出生，
    # 就跟首頁/切換器一致顯示「懷孕卡」，不能只看 session 裡
    # active_baby_id 對應的那個寶寶自己是否出生。
    if case and is_pregnancy_ongoing(case):
        menstruation_date = get_lmp_date(case)
        pregnancy_month_text = '-'
        remaining_days_text = '-'
        progress_percent = 0
        if menstruation_date:
            elapsed_days = max(0, (today - menstruation_date).days)
            pregnancy_month_text = f'第 {int(elapsed_days / 30.4375) + 1} 個月'
            expected_date = case.expecteddate or (menstruation_date + timedelta(days=280))
            remaining_days = max(0, (expected_date - today).days)
            remaining_days_text = f'剩餘 {remaining_days} 天'
            progress_percent = min(100, int((elapsed_days / 280) * 100))

        return {
            'type': 'pregnancy',
            'name': getattr(case, 'order_name', case.code),
            'icon': 'pregnant_woman',
            'subtitle': case.code,
            'pregnancy_month_text': pregnancy_month_text,
            'remaining_days_text': remaining_days_text,
            'progress_percent': progress_percent,
            'menstruation_text': menstruation_date.strftime('%Y / %m / %d') if menstruation_date else '-',
            'expecteddate_text': case.expecteddate.strftime('%Y / %m / %d') if case.expecteddate else '-',
        }

    # case 已全員出生（或找不到 case），才顯示 baby 卡
    baby = resolve_active_baby(request, current_user, fallback=True)
    if baby and baby.birthdaytime:
        birth_date = baby.birthdaytime.date()
        age_days = max(0, (today - birth_date).days)
        age_weeks = age_days // 7
        age_days_remainder = age_days % 7
        age_text = f'第 {age_weeks} 週 {age_days_remainder} 天'
        age_percent = min(100, int((age_days / 364) * 100))

        latest_baby_weight = '-'
        latest_baby_height = '-'
        weight_rec = BabyRecord.objects.filter(baby=baby).exclude(weight__isnull=True).order_by('-date', '-babyrecord_id').first()
        if weight_rec and weight_rec.weight is not None:
            latest_baby_weight = _format_number(weight_rec.weight)
        height_rec = BabyRecord.objects.filter(baby=baby).exclude(height__isnull=True).order_by('-date', '-babyrecord_id').first()
        if height_rec and height_rec.height is not None:
            latest_baby_height = _format_number(height_rec.height)

        return {
            'type': 'baby',
            'name': baby.name,
            'icon': 'face',
            'subtitle': baby.pregnancycase.code if baby.pregnancycase else '嬰兒資訊',
            'age_text': age_text,
            'age_percent': age_percent,
            'birth_date': birth_date.strftime('%Y / %m / %d'),
            'birth_height': _format_number(baby.baby_height),
            'birth_weight': _format_number(baby.baby_weight),
            'birth_head_circumference': _format_number(baby.babyheadcircumference),
            'latest_weight': latest_baby_weight,
            'latest_height': latest_baby_height,
        }

    return None


def build_family_context(request, current_user):
    """整理「家庭成員」區塊要用的資料，個人資料頁與加入碼流程共用。"""
    case = resolve_active_pregnancy_case(request, current_user)
    family_members = []
    is_case_owner = False
    if case:
        family_members = list(
            FamilyMember.objects
            .filter(pregnancycase_id=case)
            .select_related('user')
            .order_by('join_time')
        )
        _annotate_family_roles(family_members, case_owner_id=case.user_id)
        is_case_owner = (case.user_id == current_user.user_id)
        # 養育者永遠排第一，協助者也能看到誰是養育者
        owner_user = getattr(case, 'user', None)
        if owner_user:
            owner_entry = SimpleNamespace(
                user=owner_user,
                role_label='養育者',
                is_owner_member=True,
                familymember_id=None,
            )
            family_members = [owner_entry] + family_members

    # 待審申請：列出目前使用者擁有的「所有」胎數的申請。
    # 以前只看作用中的那一個 case，養育者有兩個以上胎數時，
    # 其他胎數的申請永遠不會出現在任何頁面上，等於沒人審。
    pending_members = [
        SimpleNamespace(
            applicant=req.user,
            case=req.pregnancycase,
            case_code=req.pregnancycase.code if req.pregnancycase else '',
            case_id=req.pregnancycase_id,
            join_time=req.join_time,
        )
        for req in join_request.get_requests_for_owner(current_user.user_id)
    ]

    # 自己送出、還在等待審核的申請（可以撤回）
    my_join_requests = [
        SimpleNamespace(
            case=req.pregnancycase,
            case_code=req.pregnancycase.code if req.pregnancycase else '',
            case_id=req.pregnancycase_id,
            owner_name=(req.pregnancycase.user.name if req.pregnancycase and req.pregnancycase.user else ''),
            join_time=req.join_time,
        )
        for req in join_request.get_requests_by_applicant(current_user.user_id)
    ]

    # 自己身為協助者所加入的家庭（可以退出）
    my_memberships = list(
        FamilyMember.objects
        .filter(user=current_user)
        .select_related('pregnancycase', 'pregnancycase__user')
        .order_by('join_time')
    )

    return {
        'family_members': family_members,
        'pending_members': pending_members,
        'pending_count': len(pending_members),
        'can_manage_helpers': is_case_owner,
        'is_case_owner': is_case_owner,
        'show_family_section': bool(case),
        'my_join_requests': my_join_requests,
        'my_memberships': my_memberships,
    }


def userprofile(request):
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    context = {
        'current_user': current_user,
        'latest_weight': _latest_weight_for_selection(request, current_user),
        'selected_child_info': _build_selected_child_info(request, current_user),
    }
    context.update(build_family_context(request, current_user))
    return render(request, 'user/userprofile.html', context)


def join_family(request):
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    if request.method != 'POST':
        return redirect('profile')

    join_code = request.POST.get('join_code', '').strip()

    if not join_code:
        messages.error(request, '請輸入加入碼')
    else:
        case = PregnancyCase.objects.filter(code=join_code).first()
        if not case:
            messages.error(request, f'找不到加入碼「{join_code}」，請確認是否正確')
        elif case.user_id == current_user.user_id:
            messages.error(request, '您是此胎數的建立者，無需申請。')
        elif FamilyMember.objects.filter(pregnancycase_id=case, user_id=current_user).exists():
            messages.info(request, '您已經是此胎數的協助者了')
        elif join_request.has_pending_request(case.pregnancycase_id, current_user.user_id):
            messages.info(request, '您已送出加入申請，請等待養育者審核同意。')
        else:
            join_request.add_request(case.pregnancycase_id, current_user.user_id)
            messages.success(request, '已成功送出加入申請，請等待養育者審核同意！')

    # PRG：處理完一律 redirect，避免重新整理重送申請
    return redirect('profile')


def edit_userprofile(request):
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    return render(request, 'user/edit_userprofile.html', {
        'current_user': current_user,
    })


def _validated_email_change(request, current_user, raw_email):
    """檢查要寫入的 email。

    回傳 (是否通過, 要寫入的 email)。email 沒有變更時直接通過、不做任何檢查，
    避免既有的 LINE 佔位信箱使用者一存檔就被擋住。

    為什麼要檢查：登入流程是用 email 認人（見 views/login.py），
    這裡若讓任何人隨便填別人的 email，下次對方登入就會登進這個帳號；
    填空或填錯則會讓自己下次登入變成新帳號、原本的紀錄全部失聯。
    """
    email = (raw_email or '').strip()
    current = (current_user.email or '').strip()

    if email.lower() == current.lower():
        return True, current  # 沒有變更

    if not email:
        messages.error(request, '電子郵件不可空白，這是您下次登入時辨識身分的依據。')
        return False, current

    if len(email) > 100:
        messages.error(request, '電子郵件長度不可超過 100 個字元。')
        return False, current

    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, '電子郵件格式不正確。')
        return False, current

    if email.lower().endswith(UserProfile.LINE_PLACEHOLDER_EMAIL_SUFFIX):
        messages.error(request, '這是系統保留的網域，請填寫您真正的電子郵件。')
        return False, current

    if (
        UserProfile.objects
        .filter(email__iexact=email)
        .exclude(user_id=current_user.user_id)
        .exists()
    ):
        messages.error(request, '這個電子郵件已被其他帳號使用。')
        return False, current

    return True, email


def update_profile(request):
    current_user = get_current_user_profile(request)
    if not current_user:
        return redirect('login')

    if request.method != 'POST':
        return redirect('edit_userprofile')

    name = request.POST.get('name', '').strip()
    avatar_file = request.FILES.get('avatar_file')

    email_ok, email_value = _validated_email_change(request, current_user, request.POST.get('email'))
    if not email_ok:
        # email 是登入身分的依據，驗證沒過就整筆不存，避免只存到一半
        return redirect('edit_userprofile')

    if name:
        current_user.name = name[:20]
    current_user.email = email_value

    old_avatar = current_user.avatar
    new_avatar = None
    if avatar_file:
        try:
            # 前端的 accept="image/*" 擋不住 curl，一律由後端依檔頭決定副檔名
            validate_image_upload(avatar_file)
            new_avatar = upload_image(
                avatar_file,
                folder=f'avatars/{current_user.user_id}',
            )
            current_user.avatar = new_avatar
        except InvalidImageError as e:
            messages.error(request, f'上傳頭像失敗：{e}')
            return redirect('edit_userprofile')
        except Exception:
            messages.error(request, '上傳頭像失敗，請稍後再試。')
            return redirect('edit_userprofile')

    try:
        current_user.save()
        if new_avatar and old_avatar != new_avatar:
            delete_image(old_avatar)
        messages.success(request, '個人資料已儲存')
    except IntegrityError:
        if new_avatar:
            delete_image(new_avatar)
        messages.error(request, '儲存失敗：資料與其他帳號重複，請確認電子郵件或頭像。')
    except Exception:
        if new_avatar:
            delete_image(new_avatar)
        messages.error(request, '儲存發生錯誤，請稍後再試。')

    return redirect('profile')
