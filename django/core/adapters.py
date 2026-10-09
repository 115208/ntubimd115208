import logging
from allauth.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.shortcuts import redirect
from django.urls import reverse

logger = logging.getLogger(__name__)

class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def get_connect_redirect_url(self, request, socialaccount):
        """
        當使用者在登入狀態下，成功綁定第二個社群帳號 (process=connect) 後，
        將會導向這裡回傳的 URL。預設是 socialaccount_connections，
        這裡我們把它改為導回個人資料頁面。
        """
        return reverse('profile')

    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        """
        當第三方 OAuth 驗證失敗（例如 LINE Callback 時 State 失效、網域不符或 Token 驗證失敗），
        記錄詳細 Log 並平滑導回登入頁面，避免顯示全頁 401 Unauthorized。
        """
        logger.error(
            "Social authentication error for provider=%s: error=%s, exception=%s, extra_context=%s",
            provider, error, exception, extra_context
        )
        raise ImmediateHttpResponse(redirect(reverse('login') + '?notice=social_sync_failed'))

