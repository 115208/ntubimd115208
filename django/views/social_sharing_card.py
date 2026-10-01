from django.contrib import messages
from django.shortcuts import render, redirect
from django.utils import timezone
from core.models import BabyInformation, BabyGrowthMap, BabyStatus, BabyRecord, Prenatalrecord
from views import baby_utils
from views.pregnancycase import is_pregnancy_ongoing
from views.pregnancyrecords import records_for_case
from views.session_utils import get_current_user_profile

def _format_photo_url(photo_str):
    if not photo_str:
        return ''
    photo_str = str(photo_str).strip()
    if photo_str.startswith('http://') or photo_str.startswith('https://') or photo_str.startswith('/'):
        return photo_str
    return f'/media/{photo_str}'

def social_sharing_card_view(request):
    """分享卡頁面"""
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    # 取得當前活躍寶寶
    baby = baby_utils.get_active_baby(request)

    # 權限檢查：如果沒有寶寶、寶寶尚未出生，或該胎數仍在懷孕中，關閉權限並跳警示通知
    is_pregnant = (
        not baby
        or not baby.birthdaytime
        or (hasattr(baby.birthdaytime, 'date') and baby.birthdaytime.date() > timezone.localdate())
        or (baby.pregnancycase and is_pregnancy_ongoing(baby.pregnancycase))
    )
    if is_pregnant:
        messages.warning(request, '目前胎數還在懷孕中，寶寶出生後方可使用里程碑分享卡功能！')
        return redirect('babyinformation')

    # 預設值
    baby_name = baby.name or "寶寶"
    baby_age_text = "3個月"
    baby_birthday = baby.birthdaytime.date() if baby.birthdaytime else None

    # 計算年齡 / 月齡
    if baby_birthday:
        today = timezone.localdate()
        age_days = (today - baby_birthday).days
        if age_days < 0:
            baby_age_text = "尚未出生"
        else:
            months = int(age_days / 30.4375)
            days_rem = int(age_days % 30.4375)
            if months == 0:
                baby_age_text = f"{age_days}天"
            elif days_rem == 0:
                baby_age_text = f"{months}個月"
            else:
                baby_age_text = f"{months}個月{days_rem}天"

    # 取得所有已達成的里程碑（嚴格限定此寶寶）
    completed_milestones = []
    statuses = BabyStatus.objects.filter(babyrecord__baby=baby).select_related('babyrecord', 'babygrowthmap')
    seen_milestone_ids = set()
    for s in statuses:
        mid = s.babygrowthmap.babygrowthmap_id
        if mid in seen_milestone_ids:
            continue
        seen_milestone_ids.add(mid)
        completed_milestones.append({
            'id': mid,
            'name': s.babygrowthmap.growthrecord,
            'timecourse': s.babygrowthmap.timecourse,
            'date': s.babyrecord.date.strftime('%Y-%m-%d') if s.babyrecord.date else '',
            'photo': _format_photo_url(s.babyrecord.photo),
            'record_text': s.babyrecord.record if s.babyrecord.record else '',
        })

    # 取得相片（嚴格限定當前寶寶與當前胎數，絕不抓取其他胎數的相片）
    record_photos = []
    seen_photos = set()

    # 1. 寶寶生長與里程碑紀錄相片（限定此寶寶）
    baby_records = BabyRecord.objects.filter(baby=baby).exclude(photo__isnull=True).exclude(photo='').order_by('-date')
    for r in baby_records:
        formatted_url = _format_photo_url(r.photo)
        if not formatted_url or formatted_url in seen_photos:
            continue
        seen_photos.add(formatted_url)
        status = BabyStatus.objects.filter(babyrecord=r).select_related('babygrowthmap').first()
        label = f"里程碑：{status.babygrowthmap.growthrecord}" if status else "成長紀錄"
        record_photos.append({
            'url': formatted_url,
            'label': label,
            'date': r.date.strftime('%Y-%m-%d') if r.date else '',
            'description': r.record or ''
        })

    # 2. 產檢紀錄相片 (超音波照) - 嚴格限定為當前胎數 (baby.pregnancycase) 的紀錄，絕不抓取其他胎數
    if baby.pregnancycase:
        case_preg_records = records_for_case(baby.pregnancycase)
        prenatal_records = (
            Prenatalrecord.objects.filter(pregnancyrecord__in=case_preg_records)
            .exclude(photo__isnull=True)
            .exclude(photo='')
            .select_related('pregnancyrecord')
            .order_by('-pregnancyrecord__check_date')
        )
        for pr in prenatal_records:
            formatted_url = _format_photo_url(pr.photo)
            if not formatted_url or formatted_url in seen_photos:
                continue
            seen_photos.add(formatted_url)
            chk_date = pr.pregnancyrecord.check_date.strftime('%Y-%m-%d') if (pr.pregnancyrecord and pr.pregnancyrecord.check_date) else ''
            desc = pr.pregnancyrecord.record if pr.pregnancyrecord else ''
            record_photos.append({
                'url': formatted_url,
                'label': "產檢超音波",
                'date': chk_date,
                'description': desc
            })

    context = {
        'baby': baby,
        'baby_name': baby_name,
        'baby_age_text': baby_age_text,
        'completed_milestones': completed_milestones,
        'record_photos': record_photos,
        'current_date': timezone.localdate().strftime('%Y-%m-%d'),
    }
    return render(request, 'user/social_sharing_card.html', context)


import os
import re
import base64
import binascii
import logging
import uuid
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_POST
from django.conf import settings

from views.upload_utils import InvalidImageError, validate_image_bytes

logger = logging.getLogger(__name__)

SHARING_CARD_DIR = 'sharing_cards'
# 分享頁的 filename 直接來自網址，一定要用白名單擋掉 ../ 之類的路徑穿越
SHARING_CARD_FILENAME_RE = re.compile(r'^card_[0-9a-f]{8,64}\.(?:png|jpg|gif|webp)$')


@require_POST
def upload_sharing_card(request):
    """將前端 Canvas 生成的圖卡照片上傳至伺服器媒體庫，並回傳專用分享頁面 URL 以供 LINE 分享與預覽"""
    # 這支 API 會把檔案寫進站內公開目錄，一定要登入才可以用
    user = get_current_user_profile(request)
    if not user:
        return JsonResponse({'status': 'error', 'message': '請先登入。'}, status=401)

    baby = baby_utils.get_active_baby(request)
    is_pregnant = (
        not baby
        or not baby.birthdaytime
        or (hasattr(baby.birthdaytime, 'date') and baby.birthdaytime.date() > timezone.localdate())
        or (baby.pregnancycase and is_pregnancy_ongoing(baby.pregnancycase))
    )
    if is_pregnant:
        return JsonResponse({'status': 'error', 'message': '目前胎數還在懷孕中，尚無法使用分享卡。'}, status=403)

    image_data = request.POST.get('image_data') or ''
    if not image_data.startswith('data:image') or ';base64,' not in image_data:
        return JsonResponse({'status': 'error', 'message': '無效的請求'}, status=400)

    _header, _sep, data = image_data.partition(';base64,')
    try:
        file_bytes = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return JsonResponse({'status': 'error', 'message': '圖片內容無法解碼。'}, status=400)

    # 副檔名一律由實際檔頭決定，不採用前端宣告的 mime type，
    # 避免 .html / .svg 被存到同源路徑造成儲存型 XSS
    try:
        extension = validate_image_bytes(file_bytes)
    except InvalidImageError as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    filename = f'card_{uuid.uuid4().hex}{extension}'
    media_dir = os.path.join(settings.MEDIA_ROOT, SHARING_CARD_DIR)

    try:
        os.makedirs(media_dir, exist_ok=True)
        with open(os.path.join(media_dir, filename), 'wb') as f:
            f.write(file_bytes)
    except OSError:
        logger.exception('寫入分享圖卡失敗')
        # 不要把 str(e) 回給前端，那會洩漏伺服器路徑
        return JsonResponse({'status': 'error', 'message': '圖卡儲存失敗，請稍後再試。'}, status=500)

    image_url = request.build_absolute_uri(f"{settings.MEDIA_URL}{SHARING_CARD_DIR}/{filename}")
    share_page_url = request.build_absolute_uri(f"/share_card/{filename}/")
    return JsonResponse({'status': 'success', 'image_url': image_url, 'share_page_url': share_page_url})


def share_card_detail_view(request, filename):
    """專用里程碑分享頁面，包含 OpenGraph 標籤讓 LINE 抓取圖片呈現預覽。

    這一頁維持公開（LINE 的爬蟲要能讀到 OpenGraph 標籤），
    但 filename 必須先過白名單，而且檔案不存在時回 404。
    """
    filename = os.path.basename(filename or '')
    if not SHARING_CARD_FILENAME_RE.match(filename):
        raise Http404('分享圖卡不存在')

    filepath = os.path.join(settings.MEDIA_ROOT, SHARING_CARD_DIR, filename)
    if not os.path.isfile(filepath):
        raise Http404('分享圖卡不存在')

    image_url = request.build_absolute_uri(f"{settings.MEDIA_URL}{SHARING_CARD_DIR}/{filename}")
    context = {
        'image_url': image_url,
        'filename': filename,
    }
    return render(request, 'user/share_card_detail.html', context)
