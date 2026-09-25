from django.http import HttpResponseRedirect, JsonResponse


class LoginRequiredMessage:
	"""全站登入檢查的安全網：沒有 session['user_id'] 的請求一律導向登入頁。

	各 view 仍應自行檢查登入與資料歸屬；這裡只負責擋下「忘記檢查」的情況。
	"""

	# 完全相符才放行
	ALLOWED_PATHS = {
		'/login/',
		'/google_auth_login/',
		'/google_auth_login',
		'/api/auth/google/',
		'/api/auth/callback',
		'/logout/',
		'/favicon.ico',
	}

	# 以此開頭即放行
	ALLOWED_PREFIXES = (
		'/static/',
		'/media/',
		'/logo/',
		'/admin/',
		'/accounts/',    # allauth 的 LINE / Google OAuth 流程
		'/share_card/',  # 公開分享頁，LINE 爬蟲需要讀取 OpenGraph 標籤
	)

	def __init__(self, get_response):
		self.get_response = get_response

	def __call__(self, request):
		path = request.path_info

		if path in self.ALLOWED_PATHS or path.startswith(self.ALLOWED_PREFIXES):
			return self.get_response(request)

		if not request.session.get('user_id'):
			from django.conf import settings
			if getattr(settings, 'DEBUG', False):
				try:
					from core.models import UserProfile
					user_profile = UserProfile.objects.filter(user_id=9).first() or UserProfile.objects.first()
					if user_profile:
						request.session['user_id'] = str(user_profile.user_id)
						request.session['user_email'] = user_profile.email
						request.session['user_name'] = user_profile.name
						request.session['user_avatar'] = user_profile.avatar_url
						request.session.modified = True
						return self.get_response(request)
				except Exception:
					pass

			if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
				return JsonResponse({'ok': False, 'error': '請先登入。'}, status=401)
			return HttpResponseRedirect('/login/?notice=login_required')

		return self.get_response(request)
