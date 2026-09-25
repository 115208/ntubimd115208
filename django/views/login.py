import json
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth import login as django_login
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.dispatch import receiver
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from google.auth.transport import requests
from google.oauth2 import id_token
from allauth.account.signals import user_logged_in
from allauth.socialaccount.models import SocialAccount, SocialApp
from allauth.socialaccount.signals import social_account_added

from core.models import UserProfile

logger = logging.getLogger(__name__)


def _safe_line_login_url():
    """Return the LINE login route only when there is exactly one configured LINE SocialApp.

    The allauth template tag `provider_login_url 'line'` raises `MultipleObjectsReturned`
    when duplicate `SocialApp` rows exist for the same provider. This guard keeps the page
    from crashing while still allowing the normal route when configuration is valid.
    """
    try:
        if SocialApp.objects.filter(provider='line').count() != 1:
            return ''
    except Exception:
        logger.exception('Unable to resolve LINE SocialApp while building login page')
        return ''
    return '/accounts/line/login/'


def _abort_social_login(redirect_to):
    """中止 allauth 的社群登入流程並導回指定網址。

    這裡是 signal receiver，直接 raise 一般例外會讓 allauth 的 callback 變成 500，
    而 allauth 會在 `complete_social_login()` 外層攔截 ImmediateHttpResponse，
    所以用它把流程收掉。若 allauth 版本沒有這個例外類別就安靜返回——
    使用者的 session 沒有 user_id，仍會被 LoginRequiredMessage middleware 導回登入頁。
    """
    try:
        from allauth.exceptions import ImmediateHttpResponse
    except Exception:
        logger.warning('allauth 沒有 ImmediateHttpResponse，社群登入失敗只能交給 middleware 導回登入頁')
        return
    raise ImmediateHttpResponse(redirect(redirect_to))


def _user_profile_by_email(email):
    """用 email 找帳號。

    email 目前沒有唯一鍵，資料庫裡可能同時存在多筆相同 email；
    一律 order_by('user_id') 讓「同一個 email 永遠對應到同一筆帳號」，
    避免同一個人每次登入被分到不同帳號、看到別人的資料。
    """
    email = (email or '').strip()
    if not email:
        return None
    return UserProfile.objects.filter(email__iexact=email).order_by('user_id').first()


def _resolve_user_profile_for_social_binding(request, social_account, provider, extra_data):
    """Resolve the correct UserProfile for a connect flow even when session state is missing or stale."""
    user_id = request.session.get('user_id')
    if user_id:
        user_profile = UserProfile.objects.filter(user_id=user_id).first()
        if user_profile:
            return user_profile

    auth_user = getattr(request, 'user', None)
    if getattr(auth_user, 'is_authenticated', False):
        auth_email = getattr(auth_user, 'email', '') or request.session.get('user_email', '')
        if auth_email:
            user_profile = _user_profile_by_email(auth_email)
            if user_profile:
                return user_profile

    google_email = extra_data.get('email', '') if provider == 'google' else ''
    if google_email:
        user_profile = _user_profile_by_email(google_email)
        if user_profile:
            return user_profile

    line_user_id = (extra_data.get('sub') or social_account.uid or '').strip()
    if line_user_id:
        user_profile = UserProfile.objects.filter(line_id=line_user_id).order_by('user_id').first()
        if user_profile:
            return user_profile

    return None


def _sync_django_auth_session(request, user_profile):
    """Ensure request.user is a real, authenticated Django auth user.

    Our custom login paths (GSI credential POST, LINE via allauth) all write to our own
    `UserProfile` model and to custom session keys (`user_id`, `user_email`, ...), but they
    don't always go through `django.contrib.auth.login()`. allauth's own flows -- most
    importantly the "bind account" / `process=connect` flow used on the profile page --
    rely on the real `request.user` to know who is currently logged in. Without this,
    `request.user` stays AnonymousUser even though our app considers the person logged in,
    and allauth's connect flow fails with "第三方帳號登入失敗".

    This creates/reuses a matching `auth.User` row (keyed by email) and calls the real
    Django login, so both our custom session and allauth's session agree on who is logged in.
    """
    if not user_profile.email:
        return

    User = get_user_model()
    auth_user, _ = User.objects.get_or_create(
        username=user_profile.email,
        defaults={
            'email': user_profile.email,
            'first_name': (user_profile.name or '')[:30],
        },
    )
    django_login(request, auth_user, backend='django.contrib.auth.backends.ModelBackend')


def login_page(request):
    return render(request, 'login.html', {
        'line_login_url': _safe_line_login_url(),
    })

def _next_user_id():
    max_user_id = UserProfile.objects.aggregate(max_user_id=Max('user_id')).get('max_user_id')
    return (max_user_id or 0) + 1


# user_id 是手動配號（Max + 1），兩個人同時註冊會拿到同一個號碼而撞主鍵，
# 所以插入失敗時重新算號並重試幾次。
_USER_ID_RETRY_LIMIT = 5


def _create_user_profile(name, email, line_id='', avatar=''):
    """建立新的 UserProfile，並處理手動配號造成的主鍵/唯一鍵衝突。

    avatar 欄位是 unique=True，沒有頭像時不能寫空字串（第二個人就會撞唯一鍵），
    改寫入每人專屬的佔位值；模板請用 `user.avatar_url` 顯示。
    """
    name = (name or '')[:20]
    avatar = (avatar or '')[:255]
    last_error = None
    for attempt in range(_USER_ID_RETRY_LIMIT):
        user_id = _next_user_id()
        # 第一次用第三方給的頭像；若插入失敗（有可能是頭像網址撞唯一鍵），
        # 之後的重試一律退回佔位值，至少讓帳號能順利建立。
        candidate_avatar = (avatar or '') if attempt == 0 else ''
        try:
            with transaction.atomic():
                user_profile = UserProfile(
                    user_id=user_id,
                    line_id=line_id or '',
                    name=name,
                    avatar=candidate_avatar or UserProfile.placeholder_avatar_for(user_id),
                    email=email,
                )
                user_profile.save(force_insert=True)
            return user_profile
        except Exception as exc:
            last_error = exc
            logger.warning('建立 UserProfile 失敗（user_id=%s，第 %s 次），重試中：%s', user_id, attempt + 1, exc)
    raise last_error

@receiver(user_logged_in)
def handle_allauth_login_success(request, user, **kwargs):
    social_account = SocialAccount.objects.filter(user=user).first()
    if not social_account:
        return

    provider = str(social_account.provider)
    extra_data = social_account.extra_data

    email = ''
    raw_name = ''
    picture = ''
    line_user_id = ''

    # 🌟 智慧判斷來源，不再死守 'line' 字串
    is_google = (provider == 'google')
    is_line = (provider == 'line' or provider == '2010267631' or extra_data.get('iss') == 'https://access.line.me')

    if is_google:
        email = extra_data.get('email', '')
        raw_name = extra_data.get('name', '')
        picture = extra_data.get('picture', '')
    elif is_line:
        email = extra_data.get('email', '')
        # 🎯 根據 Log 顯示，抓取 LINE 人名與頭像（支援不同 allauth provider 格式）
        raw_name = extra_data.get('name', '') or extra_data.get('displayName', '')
        picture = extra_data.get('picture', '') or extra_data.get('pictureUrl', '')
        line_user_id = extra_data.get('sub') or extra_data.get('userId') or social_account.uid

    if not email:
        email = f"{line_user_id or social_account.uid}@line.platform"
    display_name = (raw_name or email.split('@')[0])[:20]

    try:
        user_profile = _user_profile_by_email(email)
        if not user_profile and is_line and line_user_id:
            user_profile = UserProfile.objects.filter(line_id=line_user_id).order_by('user_id').first()

        if not user_profile:
            user_profile = _create_user_profile(
                name=display_name,
                email=email,
                line_id=line_user_id if is_line else '',
                avatar=picture or '',
            )
        else:
            # 已存在的 UserProfile：不覆寫 name/avatar/email 等既有資料，
            # 只有 LINE 登入且 line_id 欄位目前是空的情況下才補寫入，
            # 讓「是否已綁定 LINE」的狀態能正確判斷。
            # （Google 這邊因為是直接用 email 完全比對找到帳號，比對到時
            # email 本來就已經等於這次登入的 email，不需要再補寫。）
            if is_line and line_user_id and not user_profile.line_id:
                with transaction.atomic():
                    user_profile.line_id = line_user_id
                    user_profile.save(update_fields=['line_id'])

        request.session['user_id'] = str(user_profile.user_id)
        request.session['user_email'] = user_profile.email
        request.session['user_name'] = user_profile.name
        request.session['user_avatar'] = user_profile.avatar_url
        request.session.pop('active_case_id', None)
        request.session.pop('active_baby_id', None)
        request.session.modified = True

    except Exception as e:
        logger.error(f"社交登入同步至 UserProfile 失敗，原因: {str(e)}", exc_info=True)
        # 不可以裸 raise：這裡是 allauth 登入流程中的 signal receiver，
        # 丟出例外會直接變成 500 白畫面。改成中止登入並導回登入頁顯示錯誤。
        request.session.pop('user_id', None)
        request.session.pop('user_email', None)
        request.session.modified = True
        _abort_social_login(reverse('login') + '?notice=social_sync_failed')

# ==========================================
# 舊有的原生 Google 登入 API
# ==========================================
# 這條路由是 Google Identity Services（GSI）以 `ux_mode: 'redirect'` 跨站 POST 進來的，
# 帶不到 Django 的 csrfmiddlewaretoken，所以必須保留 csrf_exempt；
# 改用 Google 官方規定的 double-submit cookie（g_csrf_token）來擋 CSRF。
@csrf_exempt
def google_auth_login(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)

    is_json_request = (request.content_type or '').startswith('application/json')

    # --- CSRF：比對 GSI 的 g_csrf_token（cookie 與表單欄位都要有且相等） ---
    cookie_csrf = request.COOKIES.get('g_csrf_token', '')
    body_csrf = request.POST.get('g_csrf_token', '')
    if not cookie_csrf or not body_csrf or not constant_time_compare(cookie_csrf, body_csrf):
        logger.warning('GSI 登入被拒：g_csrf_token 缺少或不相符')
        return JsonResponse(
            {'status': 'error', 'message': '登入驗證失敗，請回到登入頁重新登入。'},
            status=403,
        )
    if is_json_request:
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except json.JSONDecodeError:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    else:
        payload = request.POST

    token = payload.get('token') or payload.get('credential')
    if not token:
        return JsonResponse({'status': 'error', 'message': 'Missing token'}, status=400)

    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    if not client_id:
        return JsonResponse({'status': 'error', 'message': 'Google client id is not configured'}, status=500)

    try:
        idinfo = id_token.verify_oauth2_token(token, requests.Request(), client_id)
    except ValueError:
        return JsonResponse({'status': 'error', 'message': 'Invalid token'}, status=401)

    email = idinfo.get('email', '')
    if not email:
        return JsonResponse({'status': 'error', 'message': 'Email not found in token'}, status=400)

    # 這個系統是用 email 認人的，未經 Google 驗證的 email 等於可以冒用他人帳號，一律拒絕。
    if idinfo.get('email_verified') is not True:
        logger.warning('GSI 登入被拒：email_verified 不為 True（email=%s）', email)
        return JsonResponse(
            {'status': 'error', 'message': '此 Google 帳號的電子郵件尚未通過驗證，無法登入。'},
            status=401,
        )

    name = idinfo.get('name') or email
    picture = idinfo.get('picture', '')
    name = (name or email.split('@')[0])[:50]

    try:
        # find existing user by email or by line id
        user_profile = _user_profile_by_email(email)
        if not user_profile:
            user_profile = UserProfile.objects.filter(line_id=email).order_by('user_id').first()

        if not user_profile:
            # unmanaged table: assign the next numeric user_id manually
            user_profile = _create_user_profile(
                name=name,
                email=email,
                line_id='',
                avatar=picture or '',
            )
        # 已存在的 UserProfile：不再覆寫 name/line_id/avatar 等欄位，
        # 僅在首次建立帳號時才會寫入這些從 Google 帳號取得的資訊。
    except Exception:
        logger.exception('Google 登入建立 UserProfile 失敗（email=%s）', email)
        return JsonResponse(
            {'status': 'error', 'message': '登入失敗，請稍後再試。'},
            status=500,
        )

    # 🔑 補上真正的 Django auth 登入，讓 request.user 有值。
    # 沒有這一步，allauth 的「帳號綁定 (process=connect)」流程會找不到目前登入的使用者，
    # 導致點擊「綁定 Google/LINE」時出現「第三方帳號登入失敗」。
    _sync_django_auth_session(request, user_profile)

    request.session['user_id'] = str(user_profile.user_id)
    request.session['user_email'] = user_profile.email
    request.session['user_name'] = user_profile.name
    request.session['user_avatar'] = user_profile.avatar_url
    request.session.pop('active_case_id', None)
    request.session.pop('active_baby_id', None)
    request.session.modified = True

    if is_json_request:
        return JsonResponse({
            'status': 'success',
            'email': user_profile.email,
            'name': user_profile.name,
            'user_id': str(user_profile.user_id),
            'redirect_url': reverse('index'),
        })

    return HttpResponseRedirect(reverse('index'))

# ==========================================
# 帳號綁定（在已登入狀態下，額外連結第二個社群帳號）
# ==========================================
@receiver(social_account_added)
def handle_social_account_connected(request, sociallogin, **kwargs):
    """
    當使用者已經登入，並透過 allauth 的 `?process=connect` 流程
    額外連結第二個社群帳號時（例如原本用 Google 登入，再去綁定 LINE），
    allauth 會發出 social_account_added 訊號（而不是 user_logged_in）。

    這裡會先用 session / request.user / email / LINE ID 依序找出正確的
    UserProfile，再把新的社群綁定資料寫回該筆資料，避免因為 session 失效
    或過期而錯寫到另一筆帳號，造成 Google→LINE 綁定失敗。
    """
    social_account = sociallogin.account
    provider = str(social_account.provider)
    extra_data = social_account.extra_data or {}
    user_profile = _resolve_user_profile_for_social_binding(request, social_account, provider, extra_data)
    if not user_profile:
        logger.warning(
            "social_account_added: unable to resolve matching UserProfile for provider=%s uid=%s email=%s",
            provider,
            social_account.uid,
            extra_data.get('email', ''),
        )
        return

    is_google = (provider == 'google')
    is_line = (provider == 'line' or provider == '2010267631' or extra_data.get('iss') == 'https://access.line.me')

    try:
        with transaction.atomic():
            if is_google:
                google_email = (extra_data.get('email', '') or '').strip()
                # 只有在這筆帳號目前沒有真正的 email（例如 LINE 登入產生的佔位信箱）
                # 時才補寫 Google 的 email，而且不能撞到別人的 email。
                # 直接覆寫既有 email 會讓下次用 email 認人時登入到別人的帳號。
                can_fill_email = (
                    google_email
                    and (not user_profile.email or user_profile.is_line_placeholder_email)
                    and not UserProfile.objects
                        .filter(email__iexact=google_email)
                        .exclude(user_id=user_profile.user_id)
                        .exists()
                )
                if can_fill_email:
                    user_profile.email = google_email
                    user_profile.save(update_fields=['email'])
            elif is_line and not user_profile.line_id:
                line_user_id = (extra_data.get('sub') or social_account.uid or '').strip()
                if line_user_id:
                    user_profile.line_id = line_user_id
                    user_profile.save(update_fields=['line_id'])

            request.session['user_id'] = str(user_profile.user_id)
            request.session['user_email'] = user_profile.email
            request.session['user_name'] = user_profile.name
            request.session['user_avatar'] = user_profile.avatar_url
            request.session.modified = True
    except Exception as e:
        logger.error(f"綁定社群帳號寫回 UserProfile 失敗，原因: {str(e)}", exc_info=True)


def _current_user_profile(request):
    user_id = request.session.get('user_id')
    if not user_id:
        return None
    return UserProfile.objects.filter(user_id=user_id).first()


def bind_google_account(request):
    """個人資料頁「綁定 Google」按鈕的入口，導向 allauth 的 connect 流程"""
    user_profile = _current_user_profile(request)
    if not user_profile:
        return redirect('login')

    if user_profile.google_linked:
        return redirect(f"{reverse('profile')}?perm_error=already_google")

    if not request.session.get('user_id') and not getattr(request.user, 'is_authenticated', False):
        # 對於自訂的社群登入流程，session user_id 才是實際身份來源；
        # request.user 可能尚未被 allauth 設成已驗證狀態，這時不能直接擋住綁定流程。
        return redirect(f"{reverse('profile')}?perm_error=need_relogin")

    return redirect(f"{reverse('google_login')}?process=connect")


def bind_line_account(request):
    """個人資料頁「綁定 LINE」按鈕的入口，導向 allauth 的 connect 流程"""
    user_profile = _current_user_profile(request)
    if not user_profile:
        return redirect('login')

    if user_profile.line_linked:
        return redirect(f"{reverse('profile')}?perm_error=already_line")

    if not request.session.get('user_id') and not getattr(request.user, 'is_authenticated', False):
        return redirect(f"{reverse('profile')}?perm_error=need_relogin")

    return redirect(f"{reverse('line_login')}?process=connect")


@require_POST
def logout_user(request):
    request.session.flush()
    return redirect('login')