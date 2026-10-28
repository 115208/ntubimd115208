"""使用者上傳圖片的共用驗證。

前端的 accept="image/*" 只是提示，擋不住 curl；所有上傳都必須在後端經過這裡。
副檔名一律由實際檔案內容決定，不採用使用者提供的檔名，避免 .html / .svg
被存到站內同源路徑後造成儲存型 XSS。
"""

import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage

MAX_IMAGE_BYTES = 5 * 1024 * 1024

# (副檔名, 檔頭判斷函式)
_SIGNATURES = (
	('.jpg', lambda head: head.startswith(b'\xff\xd8\xff')),
	('.png', lambda head: head.startswith(b'\x89PNG\r\n\x1a\n')),
	('.gif', lambda head: head[:6] in (b'GIF87a', b'GIF89a')),
	('.webp', lambda head: head[:4] == b'RIFF' and head[8:12] == b'WEBP'),
)

# 所有允許的副檔名，覆蓋式上傳時用來清掉同名的舊檔
_ALL_EXTENSIONS = tuple(ext for ext, _ in _SIGNATURES)

# 圖片存放在 core/static/media/<folder>/，資料庫存 /static/media/... 路徑
MEDIA_URL = '/static/media/'
_media_storage = FileSystemStorage(
	location=settings.BASE_DIR / 'core' / 'static' / 'media',
	base_url=MEDIA_URL,
)

ALLOWED_IMAGE_LABEL = 'JPG、PNG、GIF、WebP'


class InvalidImageError(ValueError):
	"""上傳的檔案不是可接受的圖片；訊息可直接顯示給使用者。"""


def detect_image_extension(head):
	"""依檔頭位元組回傳安全的副檔名；不是支援的圖片格式則回傳 None。"""
	for extension, matches in _SIGNATURES:
		if matches(head):
			return extension
	return None


def validate_image_bytes(data, max_bytes=MAX_IMAGE_BYTES):
	"""驗證記憶體中的圖片位元組（例如 base64 解碼後），回傳安全副檔名。"""
	if not data:
		raise InvalidImageError('沒有收到圖片內容。')
	if len(data) > max_bytes:
		raise InvalidImageError(f'圖片大小不可超過 {max_bytes // (1024 * 1024)}MB。')
	extension = detect_image_extension(data[:16])
	if not extension:
		raise InvalidImageError(f'只接受 {ALLOWED_IMAGE_LABEL} 格式的圖片。')
	return extension


def validate_image_upload(uploaded_file, max_bytes=MAX_IMAGE_BYTES):
	"""驗證 request.FILES 取得的檔案，回傳安全副檔名；不合格則丟 InvalidImageError。"""
	if not uploaded_file:
		raise InvalidImageError('沒有收到圖片檔案。')
	if uploaded_file.size > max_bytes:
		raise InvalidImageError(f'圖片大小不可超過 {max_bytes // (1024 * 1024)}MB。')

	uploaded_file.seek(0)
	head = uploaded_file.read(16)
	uploaded_file.seek(0)

	extension = detect_image_extension(head)
	if not extension:
		raise InvalidImageError(f'只接受 {ALLOWED_IMAGE_LABEL} 格式的圖片。')
	return extension


def safe_image_name(extension, prefix=''):
	"""產生不含使用者輸入的檔名。"""
	return f'{prefix}{uuid.uuid4().hex}{extension}'


def upload_image(uploaded_file, folder, name=None, overwrite=False):
	"""驗證後存到 core/static/media/<folder>/，回傳網址路徑；沒有檔案回傳 None。
	name 為不含副檔名的檔名（由程式產生，例如 user_id）；不給則用隨機檔名。
	overwrite=True 且有給 name 時，會先刪除同名的所有副檔名舊檔
	（例如存 1.jpg 前先刪 1.png、1.jpg ...），檔名固定、不會加隨機字尾。
	overwrite=False 時，若同名檔案已存在，Django 會自動加隨機字尾，不會覆蓋舊檔。"""
	if not uploaded_file:
		return None
	extension = validate_image_upload(uploaded_file)  # 先驗證，不合格就不會動到舊檔

	if overwrite and name:
		for ext in _ALL_EXTENSIONS:
			old_path = f'{folder}/{name}{ext}'
			if _media_storage.exists(old_path):
				_media_storage.delete(old_path)

	filename = f'{name}{extension}' if name else safe_image_name(extension)
	saved = _media_storage.save(f'{folder}/{filename}', uploaded_file)
	return _media_storage.url(saved)


def delete_image(url):
	"""刪除本機圖片；空值、舊的 http(s) 網址、media 以外的路徑一律略過。"""
	if url and str(url).startswith(MEDIA_URL):
		_media_storage.delete(str(url)[len(MEDIA_URL):])