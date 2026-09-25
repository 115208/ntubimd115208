import datetime
import calendar
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils import timezone
from core.models import BabyRecord, BabyGrowthMap, BabyStatus
from views.pregnancycase import resolve_active_baby
from views.session_utils import get_current_user_profile
from views.upload_utils import InvalidImageError, safe_image_name, validate_image_upload

def get_active_baby(request):
    """取得當前 Session 活躍的寶寶"""
    return resolve_active_baby(request, get_current_user_profile(request))

def parse_float(value):
    """安全地將數值轉成浮點數"""
    try: return float(value)
    except (TypeError, ValueError): return None

# ── 出生體徵合理範圍（單位：體重 kg，其餘 cm）──
BIRTH_VITAL_RANGES = {
    'baby_weight':           (0.3,  6.0,  '出生體重（kg）合理範圍為 0.3 ~ 6.0 kg'),
    'baby_height':           (25.0, 65.0, '出生身長（cm）合理範圍為 25 ~ 65 cm'),
    'babyheadcircumference': (25.0, 45.0, '出生頭圍（cm）合理範圍為 25 ~ 45 cm'),
    'chestcircumference':    (20.0, 42.0, '出生胸圍（cm）合理範圍為 20 ~ 42 cm'),
}

def validate_birth_vitals(weight_kg, height_cm, head_cm, chest_cm):
    """驗證出生體徵數値是否在合理範圍內。回傳 None 代表合法；否則回傳錯誤訊息。"""
    pairs = [
        (weight_kg, 'baby_weight'),
        (height_cm, 'baby_height'),
        (head_cm,   'babyheadcircumference'),
        (chest_cm,  'chestcircumference'),
    ]
    for value, key in pairs:
        if value is None:
            continue
        lo, hi, msg = BIRTH_VITAL_RANGES[key]
        if not (lo <= value <= hi):
            return msg
    return None

def get_birth_week(baby):
    #計算出生週數、進行合理性檢查：
    if not baby or not baby.birthdaytime or not baby.pregnancycase or not baby.pregnancycase.menstruation:
        return None
    
    # 統一轉換為 date 型別
    birth_date = baby.birthdaytime.date() if hasattr(baby.birthdaytime, 'date') else baby.birthdaytime
    lmp_date = baby.pregnancycase.menstruation
    
    # 防禦一：出生日不可大於今天（一律以 Asia/Taipei 當地日期為準）
    if birth_date > timezone.localdate():
        return None
        
    delta = birth_date - lmp_date
    
    # 防禦二：出生日不可小於或等於 LMP。
    # 醫學下限：22週（154天）是目前公認的胎兒體外存活最低週數；
    # 低於此週數在臨床上屬死產或流產，不會有活產體徵紀錄，視為亂填。
    if delta.days < 154:
        return None

    weeks = delta.days // 7
    days  = delta.days % 7
    
    return f'{weeks}w{days}d' if days else f'{weeks}w'

def split_note_and_milestones(record):
    """分離紀錄內文與綁定的里程碑清單"""
    if not record: return [], ""
    milestones = list(BabyStatus.objects.filter(babyrecord=record).values_list('babygrowthmap__growthrecord', flat=True))
    return milestones, str(record.record or "")

def calculate_age_in_months(birthdaytime, record_date):
    """精確計算月齡"""
    if not birthdaytime or not record_date: return None
    def _to_date(value):
        if isinstance(value, datetime.datetime): return value.date()
        if isinstance(value, datetime.date): return value
        try: return datetime.date.fromisoformat(str(value)[:10])
        except Exception: return None

    birth_date, rec_date = _to_date(birthdaytime), _to_date(record_date)
    if birth_date is None or rec_date is None: return None
    if rec_date < birth_date: return 0
    years = rec_date.year - birth_date.year
    months = rec_date.month - birth_date.month
    if rec_date.day < birth_date.day:
        months -= 1
    if months < 0:
        years -= 1
        months += 12
    return years * 12 + months

def get_relevant_timecourses(age_in_months):
    """根據月齡推薦發展指標區間時間軸代碼"""
    if age_in_months is None: return None
    if age_in_months <= 0: age_in_months = 1
    if age_in_months <= 11:
        m = max(1, age_in_months)
        return sorted({max(1, m - 1), m, m + 1})
    elif age_in_months == 12: return [11, 12, 18]
    elif age_in_months < 18: return [12, 18]
    elif age_in_months == 18: return [12, 18, 24]
    elif age_in_months < 24: return [18, 24]
    elif age_in_months == 24: return [18, 24, 36]
    elif age_in_months < 36: return [24, 36]
    else: return [36]

def save_uploaded_image(image_file):
    """上傳圖片至儲存區，回傳相對 URL。

    安全性：副檔名一律由實際檔頭決定、檔名由系統產生，
    絕不沿用使用者提供的 image_file.name —— 否則上傳 .html / .svg
    存到站內同源路徑後會造成儲存型 XSS。
    檔案不是可接受的圖片時丟出 InvalidImageError（訊息可直接顯示給使用者），
    呼叫端必須捕捉並回到表單顯示錯誤。
    """
    if not image_file: return None
    extension = validate_image_upload(image_file)
    storage = FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)
    filename = storage.save(f'baby_records/{safe_image_name(extension)}', image_file)
    return storage.url(filename)


def build_growth_timeline_context(baby):
    """
    核心架構重構：統一處理寶寶成長里程碑與舊資料文字向下相容的 Context 產生器
    """
    if not baby:
        return {"growth_timeline": [], "growth_owner_name": "寶寶"}

    growth_maps = BabyGrowthMap.objects.all().order_by('timecourse')
    
    # 1. 取得已完成里程碑關聯表
    completed_statuses = {
        status.babygrowthmap_id: status.babyrecord
        for status in BabyStatus.objects.filter(babyrecord__baby=baby).select_related('babyrecord')
    }
    
    # 2. 取得歷史紀錄文字雜湊（向下相容舊資料）
    milestone_text_set = {}
    for rec in BabyRecord.objects.filter(baby=baby):
        m, _ = split_note_and_milestones(rec)
        for name in m:
            milestone_text_set[name] = rec
        
    # 3. 建立統一的 Timeline 資料結構
    growth_timeline = []
    for g_map in growth_maps:
        record = completed_statuses.get(g_map.babygrowthmap_id)
        if not record and g_map.growthrecord in milestone_text_set:
            record = milestone_text_set[g_map.growthrecord]
            
        is_completed = record is not None
        achieved_date = record.date.strftime('%Y/%m/%d') if record else ""
        photo = record.photo if record else None
        
        growth_timeline.append({
            "map_id": g_map.babygrowthmap_id,
            "timecourse": g_map.timecourse,
            "growthrecord": g_map.growthrecord,
            "status": "completed" if is_completed else "pending",
            "description": "", 
            "category": "",   
            "photo": photo,
            "achieved_date": achieved_date
        })
        
    return {
        "growth_timeline": growth_timeline,
        "growth_owner_name": getattr(baby, 'name', '寶寶')
    }


# mom_records 與 growth_assistant 支援 off（完全隱藏媽媽紀錄 / 關閉成長評估助手）；其他功能只有 view/edit
FEATURE_KEYS = ('baby_records', 'mom_records', 'care_records', 'growth_assistant')
PERMISSION_LEVELS = ('off', 'view', 'edit')

# features 中，mom_records 與 growth_assistant 的 off 真正有效（拒絕查看／使用）；
# 其他 feature 的 off 視為 view（向下相容舊資料）
_OFF_BLOCKS_VIEW = {'mom_records', 'growth_assistant'}

def get_permission(member, feature, default='view'):
    """讀取某位協助者對某功能的權限等級。
    - member 為 None（非擁有者且無 FamilyMember 列）→ 'off'，一律拒絕（fail closed）
    - mom_records、growth_assistant 的 off 代表「不可查看／不可使用」
    - 其他 feature 的 off 向下相容視為 view

    注意：default 只用於「有 FamilyMember 列、但 permissions 缺這個 key」的舊資料，
    不再用於「完全沒有成員關係」的情境。所有呼叫點都已先判斷個案擁有者並短路回 True，
    擁有者不會走到這裡，因此不會被擋住。
    """
    if member is None:
        return 'off'
    value = (member.permissions or {}).get(feature, default)
    if value not in PERMISSION_LEVELS:
        return default
    # 非 _OFF_BLOCKS_VIEW 的 off 轉為 view
    if value == 'off' and feature not in _OFF_BLOCKS_VIEW:
        return 'view'
    return value

def has_permission(member, feature, required='view', default='view'):
    """required='view'：view/edit 通過，off 拒絕。
    required='edit'：只有 edit 通過。"""
    level = get_permission(member, feature, default=default)
    if required == 'edit':
        return level == 'edit'
    # required='view'：off 拒絕
    return level in ('view', 'edit')