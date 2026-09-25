import calendar
import random
import string
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from core.models import BabyInformation, PregnancyCase, FamilyMember
from views.session_utils import get_current_user_profile


# --- 共用小工具 ---
def _ordered_babies(case):
    """取得依 baby_id 排序的嬰幼兒清單。

    一律走 `.all()`，這樣在外層有 prefetch_related('babyinformation_set') 時
    可以直接用快取，不會每個 case 再打一次資料庫（N+1）。
    """
    if not case:
        return []
    return sorted(case.babyinformation_set.all(), key=lambda b: b.baby_id)


def _local_birth_datetime(birthdaytime):
    """把帶時區的出生時間轉成當地（Asia/Taipei）時間。

    資料庫存的是 UTC，直接 strftime 會少 8 小時；
    表單回填與顯示都必須先轉成當地時間。
    """
    if isinstance(birthdaytime, datetime) and timezone.is_aware(birthdaytime):
        return timezone.localtime(birthdaytime)
    return birthdaytime


def _local_birth_date(birthdaytime):
    """出生時間的當地日期（None 安全）。"""
    local = _local_birth_datetime(birthdaytime)
    if local is None:
        return None
    return local.date() if isinstance(local, datetime) else local


def _member_case_ids(user, request=None):
    """該使用者以協助者身分可存取的 pregnancycase_id 集合。

    同一個 request 內會快取，避免 baby_switcher / resolve_* 反覆 exists() 查詢。
    """
    if not user:
        return set()
    if request is not None:
        cached = getattr(request, '_member_case_ids_cache', None)
        if cached is not None and cached[0] == user.user_id:
            return cached[1]
    ids = set(
        FamilyMember.objects.filter(user=user).values_list('pregnancycase_id', flat=True)
    )
    if request is not None:
        request._member_case_ids_cache = (user.user_id, ids)
    return ids


def _user_can_access_case(user, case, request=None):
    """擁有者或該胎數的協助者才算有權限。"""
    if not user or not case:
        return False
    if case.user_id == user.user_id:
        return True
    return case.pregnancycase_id in _member_case_ids(user, request)


# --- Pregnancy status (gestation, ongoing vs born) ---
# 經期推算
def get_lmp_date(case):
    """Last menstrual period date, or estimated from expected due date."""
    if case.menstruation:
        return case.menstruation
    if case.expecteddate:
        return case.expecteddate - timedelta(days=280)
    return None


# 出生時間合理性驗證（供 add_pregnancy_case / edit_pregnancy_case / baby_information 共用）
def validate_birth_datetime(lmp_date, birth_dt, on_date=None):
    """
    驗證出生時間是否符合生理合理範圍：
      - 不可為未來日期
      - 不可早於 LMP 後 22 週（154 天）── 醫學公認最低體外存活週數
      - 不可晚於 LMP 後 43 週（301 天）
    回傳 None 代表合法；否則回傳可直接顯示給使用者的錯誤訊息字串。
    birth_dt 為 None（尚未出生）一律視為合法。
    """
    if birth_dt is None:
        return None

    on_date = on_date or timezone.localdate()
    birth_date = birth_dt.date() if hasattr(birth_dt, 'date') else birth_dt

    if birth_date > on_date:
        return '出生日期檢查：出生時間不能是未來日期'

    if lmp_date:
        delta_days = (birth_date - lmp_date).days
        if delta_days < 154:  # 22週：醫學公認最低存活週數
            return '資料異常：出生日期不可早於懷孕 22 週，請檢查最後一次月經或出生日設定'
        if delta_days > 301:  # 43週
            return '資料異常：懷孕週數超過 43 週，不符合正常生理上限'
    return None


# 懷孕進度
def get_gestation_parts(case, on_date=None):
    """以 LMP 為基準計算目前懷孕週數、天數與進度百分比。"""
    on_date = on_date or timezone.localdate()
    lmp = get_lmp_date(case)
    if not lmp:
        return None
    # timezone-aware datetime 傳入時需先轉 date，否則減法會型別錯誤
    if not isinstance(on_date, type(lmp)):
        try:
            on_date = on_date.date() if hasattr(on_date, 'date') else on_date
        except Exception:
            return None
    delta = on_date - lmp
    if delta.days < 0:
        return None
    # 臨床慣例：LMP 當天為 0w0d（與 baby_utils.get_birth_week 一致）；
    # 上限 42w 防止超預產期後顯示異常大數字
    weeks = min(42, delta.days // 7)
    days = delta.days % 7
    progress_percent = min(100, max(0, round(delta.days / 280 * 100)))
    return {
        'weeks': weeks,
        'days': days,
        'total_days': delta.days,
        'progress_percent': progress_percent,
        'is_overdue': delta.days > 294,
    }


def get_gestation_text(case, on_date=None):
    """Weeks + days since LMP for an ongoing pregnancy."""
    parts = get_gestation_parts(case, on_date)
    if not parts:
        return "未知週數"
    return f"{parts['weeks']}w {parts['days']}d"


def get_pregnancy_status(case, on_date=None):
    """
    回傳 'pregnant' | 'overdue' | 'born'。
    overdue = 仍有未出生嬰幼兒且已超預產期；born = 所有嬰幼兒皆已出生。
    """
    on_date = on_date or timezone.localdate()
    babies = _ordered_babies(case)

    if babies:
        # 所有嬰幼兒都有出生日期才算 born；否則繼續判斷是否超期
        if all(b.birthdaytime is not None for b in babies):
            return 'born'

    # Bug 修正：overdue 以實際 expecteddate + 14 天為判斷基準，
    # 而非固定用 lmp+294（若醫生手動設定與 lmp+280 不同的 expecteddate，兩者結果不同）
    due = case.expecteddate
    if not due:
        lmp = get_lmp_date(case)
        due = lmp + timedelta(days=280) if lmp else None
    if due and on_date > due + timedelta(days=14):
        return 'overdue'
    return 'pregnant'


def is_pregnancy_ongoing(case, on_date=None):
    """pregnant/overdue 都屬於「進行中」，born 才算結束。"""
    return get_pregnancy_status(case, on_date) in ('pregnant', 'overdue')


def get_case_display_baby(case):
    """Baby shown in pregnancy UI: first without birth date, else first baby."""
    babies = _ordered_babies(case)
    if not babies:
        return None
    for baby in babies:
        if baby.birthdaytime is None:
            return baby
    return babies[0]


# 年齡計算
def baby_age_text(birthdaytime, on_date=None):
    if not birthdaytime:
        return ""
    on_date = on_date or timezone.localdate()
    # 先轉當地時間再取日期，否則 UTC 會讓凌晨出生的寶寶少算一天
    bday = _local_birth_date(birthdaytime)
    if not bday:
        return ""

    years = on_date.year - bday.year
    months = on_date.month - bday.month
    if on_date.day < bday.day:
        months -= 1
    if months < 0:
        years -= 1
        months += 12
    return f"{years}歲 {months}個月"


def partition_pregnancy_cases(cases, on_date=None):
    """Split cases into ongoing pregnancies and born babies."""
    on_date = on_date or timezone.localdate()
    ongoing_cases = []
    born_babies = []

    for case in cases:
        status = get_pregnancy_status(case, on_date)
        if status in ('pregnant', 'overdue'):
            case.gestation_text = get_gestation_text(case, on_date)
            case.display_baby = get_case_display_baby(case)
            case.is_overdue = (status == 'overdue')
            # 修正：每個寶寶各自標記是否已出生，模板才能分開顯示
            all_babies = _ordered_babies(case)
            for b in all_babies:
                if b.birthdaytime:
                    b.age_text = baby_age_text(b.birthdaytime, on_date)
            case.all_babies = all_babies
            ongoing_cases.append(case)
            continue

        for baby in _ordered_babies(case):
            if not baby.birthdaytime:
                continue
            baby._case_id = case.pregnancycase_id
            baby.age_text = baby_age_text(baby.birthdaytime, on_date)
            baby.birthday_str = _local_birth_date(baby.birthdaytime)
            if hasattr(baby.birthday_str, "strftime"):
                baby.birthday_str = baby.birthday_str.strftime("%Y-%m-%d")
            born_babies.append(baby)

    return ongoing_cases, born_babies


# --- Active selection & header switcher ---


def sync_active_selection_from_request(request, user=None):
    """Apply ?case_id= / ?baby_id= to session (must run in views before reading session)."""
    # 安全性：沒有 user 就無法做歸屬檢查，一律不採用請求中的 id，
    # 否則任何人帶 ?case_id= 就能把別人的個案塞進自己的 session。
    if not user:
        return

    baby_id_param = request.GET.get('baby_id')
    case_id_param = request.GET.get('case_id')
    changed = False

    if baby_id_param:
        try:
            baby_qs = BabyInformation.objects.select_related('pregnancycase').filter(
                baby_id=int(baby_id_param),
            )
            baby_obj = baby_qs.first()
            if baby_obj and baby_obj.pregnancycase_id and _user_can_access_case(user, baby_obj.pregnancycase, request):
                request.session['active_baby_id'] = baby_obj.baby_id
                request.session['active_case_id'] = baby_obj.pregnancycase_id
                changed = True
        except (ValueError, TypeError):
            pass

    if case_id_param:
        try:
            case_id = int(case_id_param)
            case_qs = PregnancyCase.objects.filter(pregnancycase_id=case_id)
            case_obj = case_qs.first()
            if case_obj and _user_can_access_case(user, case_obj, request):
                request.session['active_case_id'] = case_id
                request.session.pop('active_baby_id', None)
                changed = True
        except (ValueError, TypeError):
            pass

    if changed:
        request.session.modified = True


def _case_for_user(user, case_id):
    return PregnancyCase.objects.filter(pregnancycase_id=case_id, user=user).first()


def resolve_active_pregnancy_case(request, user):
    """Active懷孕紀錄: honors ?case_id= / ?baby_id=, then session."""
    if not user:
        return None

    def _get_all_cases():
        # prefetch 嬰幼兒，讓後續 get_pregnancy_status / get_case_display_baby 不再逐筆查詢
        own_cases = list(
            PregnancyCase.objects.filter(user=user).prefetch_related('babyinformation_set')
        )
        member_ids = _member_case_ids(user, request)
        shared = list(
            PregnancyCase.objects.filter(pregnancycase_id__in=member_ids)
            .prefetch_related('babyinformation_set')
        ) if member_ids else []
        cases_dict = {c.pregnancycase_id: c for c in own_cases + shared}
        return sorted(cases_dict.values(), key=lambda c: c.create_time)

    def _case_for_user_ext(case_id):
        case = PregnancyCase.objects.filter(
            pregnancycase_id=case_id
        ).prefetch_related('babyinformation_set').first()
        if _user_can_access_case(user, case, request):
            return case
        return None

    sync_active_selection_from_request(request, user)

    case_id_param = request.GET.get('case_id')
    if case_id_param:
        try:
            case = _case_for_user_ext(int(case_id_param))
            if case:
                return case
        except (ValueError, TypeError):
            pass

    baby_id_param = request.GET.get('baby_id')
    if baby_id_param:
        try:
            baby = BabyInformation.objects.select_related('pregnancycase').filter(
                baby_id=int(baby_id_param),
            ).first()
            if baby and _user_can_access_case(user, baby.pregnancycase, request):
                return baby.pregnancycase
        except (ValueError, TypeError):
            pass

    active_baby_id = request.session.get('active_baby_id')
    if active_baby_id:
        try:
            baby = BabyInformation.objects.select_related('pregnancycase').filter(
                baby_id=int(active_baby_id),
            ).first()
            if baby and _user_can_access_case(user, baby.pregnancycase, request):
                request.session['active_case_id'] = baby.pregnancycase_id
                return baby.pregnancycase
        except (ValueError, TypeError):
            pass

    # 修正：session['active_case_id'] 多處寫入卻從來沒被讀回來，
    # 導致切換到「尚未有寶寶的懷孕中個案」後，下一個沒帶參數的請求又跳回舊個案。
    active_case_id = request.session.get('active_case_id')
    if active_case_id:
        try:
            case = _case_for_user_ext(int(active_case_id))
            if case:
                return case
        except (ValueError, TypeError):
            pass

    cases = _get_all_cases()
    # fallback 順序：先找進行中的懷孕（最新的一筆），沒有才退回最近一筆已出生的個案
    ongoing = [c for c in cases if is_pregnancy_ongoing(c)]
    if ongoing:
        case = ongoing[-1]
        request.session['active_case_id'] = case.pregnancycase_id
        if not _ordered_babies(case):
            # 這個個案還沒有任何寶寶，清掉殘留的 active_baby_id 才不會互相打架
            request.session.pop('active_baby_id', None)
        request.session.modified = True
        return case

    for case in reversed(cases):
        if any(b.birthdaytime for b in _ordered_babies(case)):
            request.session['active_case_id'] = case.pregnancycase_id
            b = get_case_display_baby(case)
            if b:
                request.session['active_baby_id'] = b.baby_id
            request.session.modified = True
            return case

    if cases:
        case = cases[-1]
        request.session['active_case_id'] = case.pregnancycase_id
        request.session.modified = True
        return case

    return None


def resolve_active_baby(request, user, *, fallback=True):
    """Active嬰幼兒: honors ?baby_id=, then session; optional first-baby fallback."""
    if not user:
        return None

    sync_active_selection_from_request(request, user)

    for param in ('baby_id', 'id'):
        raw = request.GET.get(param) or request.POST.get(param)
        if raw:
            try:
                baby = BabyInformation.objects.select_related('pregnancycase').filter(
                    baby_id=int(raw),
                ).first()
                if baby and _user_can_access_case(user, baby.pregnancycase, request):
                    request.session['active_baby_id'] = baby.baby_id
                    if baby.pregnancycase_id:
                        request.session['active_case_id'] = baby.pregnancycase_id
                    request.session.modified = True
                    return baby
            except (ValueError, TypeError):
                pass

    active_baby_id = request.session.get('active_baby_id')
    if active_baby_id:
        baby = BabyInformation.objects.select_related('pregnancycase').filter(
            baby_id=active_baby_id,
        ).first()
        if baby and _user_can_access_case(user, baby.pregnancycase, request):
            return baby

    if not fallback:
        return None

    case = resolve_active_pregnancy_case(request, user)
    baby = get_case_display_baby(case) if case else None

    if baby:
        request.session['active_baby_id'] = baby.baby_id
        if baby.pregnancycase_id:
            request.session['active_case_id'] = baby.pregnancycase_id
        request.session.modified = True
    return baby


def build_active_selection_query(request):
    """Query string fragment to keep the current child/case across pages."""
    if request.session.get('active_baby_id'):
        return urlencode({'baby_id': request.session['active_baby_id']})
    if request.session.get('active_case_id'):
        return urlencode({'case_id': request.session['active_case_id']})
    return ''


def url_with_active_selection(request, path, extra_params=None):
    """Build path + active baby/case (+ optional date etc.)."""
    params = {}
    if request.session.get('active_baby_id'):
        params['baby_id'] = request.session['active_baby_id']
    elif request.session.get('active_case_id'):
        params['case_id'] = request.session['active_case_id']
    if extra_params:
        params.update(extra_params)
    if not params:
        return path
    joiner = '&' if '?' in path else '?'
    return f'{path}{joiner}{urlencode(params)}'


def build_switcher_target_url(request, item):
    """Preserve current path and date filter; switch active case or baby."""
    params = {}
    for key in ('date',):
        value = request.GET.get(key)
        if value:
            params[key] = value

    if item.get('is_baby'):
        params['baby_id'] = item['baby_id']
    else:
        params['case_id'] = item['case_id']

    query = urlencode(params)
    path = request.path or '/'
    return f'{path}?{query}' if query else path


def build_pregnancy_progress(case, on_date=None):
    """Progress card payload for the index page."""
    if not case:
        return {'show_card': False, 'is_ongoing': False}

    display_baby = get_case_display_baby(case)
    baby_name = display_baby.name if display_baby else ''

    #懷孕中對每個寶寶各建一筆 baby-level item
    if is_pregnancy_ongoing(case):
        parts = get_gestation_parts(case, on_date)
        if not parts:
            return {
                'show_card': True,
                'is_ongoing': True,
                'baby_name': baby_name,
                'weeks': None,
                'days': None,
                'progress_percent': 0,
                'status_note': '請填寫最後月經或預產期以顯示週數',
            }

        return {
            'show_card': True,
            'is_ongoing': True,
            'baby_name': baby_name,
            'weeks': parts['weeks'],
            'days': parts['days'],
            'progress_percent': parts['progress_percent'],
            'status_note': '',
        }

    baby = display_baby
    if baby and baby.birthdaytime:
        # 計算寶寶月齡相對3歲（36個月）的進度
        bday = _local_birth_date(baby.birthdaytime)
        on_date_val = on_date or timezone.localdate()

        years = on_date_val.year - bday.year
        months = on_date_val.month - bday.month
        days_remainder = on_date_val.day - bday.day
        if days_remainder < 0:
            months -= 1
            days_in_prev_month = (on_date_val.replace(day=1) - timedelta(days=1)).day
            days_remainder += days_in_prev_month
        if months < 0:
            years -= 1
            months += 12
        total_months = years * 12 + months

        baby_progress_percent = min(100, max(0, round(total_months / 36 * 100)))

        return {
            'show_card': True,
            'is_ongoing': False,
            'baby_name': baby.name,
            'status_note': f'已出生 · {baby_age_text(baby.birthdaytime, on_date)}',
            'weeks': total_months,
            'days': max(0, days_remainder),
            'progress_percent': baby_progress_percent,
        }

    return {
        'show_card': True,
        'is_ongoing': False,
        'baby_name': baby_name,
        'status_note': '此懷孕紀錄已完成',
        'weeks': None,
        'days': None,
        'progress_percent': 100,
    }


def baby_switcher(request):
    """Context processor: baby/pregnancy switcher in base.html."""
    user = get_current_user_profile(request)
    if not user:
        return {}

    sync_active_selection_from_request(request, user)

    # prefetch 嬰幼兒，避免每個 case 都各自再查一次（原本每筆 case 至少 3 次查詢）
    cases_own = list(
        PregnancyCase.objects.filter(user=user).prefetch_related('babyinformation_set')
    )
    member_ids = _member_case_ids(user, request)
    shared = list(
        PregnancyCase.objects.filter(pregnancycase_id__in=member_ids)
        .prefetch_related('babyinformation_set')
    ) if member_ids else []
    cases_dict = {c.pregnancycase_id: c for c in cases_own + shared}
    cases = sorted(cases_dict.values(), key=lambda c: c.create_time)

    switcher_items = []
    current_date = timezone.localdate()
    own_case_ids = {c.pregnancycase_id for c in cases_own}

    for case in cases:
        is_own = case.pregnancycase_id in own_case_ids
        display_baby = get_case_display_baby(case)
        baby_name = display_baby.name if display_baby else '懷孕紀錄'

        if is_pregnancy_ongoing(case):
            gestation = get_gestation_text(case, current_date)
            gestation_text = (
                f"懷孕中 ({gestation.split()[0]})"
                if gestation != "未知週數"
                else "懷孕中"
            )
            babies = _ordered_babies(case)
            if not babies:
                item = {
                    'is_baby': False,
                    'is_own': is_own,
                    'name': baby_name,
                    'desc': gestation_text,
                    'icon': 'pregnant_woman',
                    'case_id': case.pregnancycase_id,
                }
                item['url'] = build_switcher_target_url(request, item)
                switcher_items.append(item)
            else:
                for baby in babies:
                    # 修正：case 整體還在懷孕中（有手足未出生），
                    # 但個別寶寶自己可能已經出生 —— 這裡要各自判斷，
                    # 不能整批套用 case 層級的 gestation_text，
                    # 否則已出生的寶寶會被切換器誤標成「懷孕中」。
                    if baby.birthdaytime:
                        item = {
                            'is_baby': True,
                            'is_own': is_own,
                            'name': baby.name,
                            'desc': baby_age_text(baby.birthdaytime, current_date),
                            'icon': 'face',
                            'baby_id': baby.baby_id,
                            'case_id': case.pregnancycase_id,
                        }
                    else:
                        item = {
                            'is_baby': True,
                            'is_own': is_own,
                            'name': baby.name,
                            'desc': gestation_text,
                            'icon': 'pregnant_woman',
                            'baby_id': baby.baby_id,
                            'case_id': case.pregnancycase_id,
                        }
                    item['url'] = build_switcher_target_url(request, item)
                    switcher_items.append(item)
        else:
            for baby in _ordered_babies(case):
                if baby.birthdaytime:
                    item = {
                        'is_baby': True,
                        'is_own': is_own,
                        'name': baby.name,
                        'desc': baby_age_text(baby.birthdaytime, current_date),
                        'icon': 'face',
                        'baby_id': baby.baby_id,
                        'case_id': case.pregnancycase_id,
                    }
                    item['url'] = build_switcher_target_url(request, item)
                    switcher_items.append(item)

    active_baby_id = request.session.get('active_baby_id')
    active_case_id = request.session.get('active_case_id')

    if '/babyinformation' in request.path or '/babyrecord' in request.path or '/babygrowthmap' in request.path:
        switcher_items = [item for item in switcher_items if item.get('is_baby')]

    active_item = None
    if active_baby_id:
        active_item = next(
            (item for item in switcher_items if item.get('is_baby') and item.get('baby_id') == active_baby_id),
            None,
        )
    if not active_item and active_case_id:
        active_item = next(
            (item for item in switcher_items if item.get('case_id') == active_case_id),
            None,
        )
    if not active_item and switcher_items:
        active_item = switcher_items[0]

    switcher_items_own = [i for i in switcher_items if i.get('is_own')]
    switcher_items_shared = [i for i in switcher_items if not i.get('is_own')]

    return {
        'switcher_items': switcher_items,
        'switcher_items_own': switcher_items_own,
        'switcher_items_shared': switcher_items_shared,
        'switcher_active_item': active_item,
        'active_case_id': active_case_id,
        'active_baby_id': active_baby_id,
        'active_selection_query': build_active_selection_query(request),
    }

# --- Pregnancy case CRUD views ---
def _calculate_expected_date(menstruation):
    """EDD from LMP: Naegele's rule (month −3, day +7, year +1), same as +280 days."""
    if not menstruation:
        return None
    year = menstruation.year + 1
    month = menstruation.month - 3
    if month <= 0:
        month += 12
        year -= 1
    day = min(menstruation.day, calendar.monthrange(year, month)[1])
    due = menstruation.replace(year=year, month=month, day=day) + timedelta(days=7)
    return due


def _calculate_lmp_from_due(expecteddate):
    """LMP from EDD：Naegele's rule 反推（月＋3、日－7、年－1），為 _calculate_expected_date 的反函式。"""
    if not expecteddate:
        return None
    year = expecteddate.year - 1
    month = expecteddate.month + 3
    if month > 12:
        month -= 12
        year += 1
    day = min(expecteddate.day, calendar.monthrange(year, month)[1])
    lmp = expecteddate.replace(year=year, month=month, day=day) - timedelta(days=7)
    return lmp


def _generate_unique_code():
    """Generate a unique 6-character alphanumeric join code."""
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if not PregnancyCase.objects.filter(code=code).exists():
            return code


def _parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pregnancy_case(request):
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    # 刪除胎數：僅接受 POST，避免 GET 連結（含瀏覽器預抓取）誤觸刪除。
    # 前端範本需將刪除按鈕改成 <form method="post"> 提交 delete_id，而非 <a href="?delete_id=...">。
    if request.method == 'POST' and request.POST.get('delete_id'):
        delete_id = request.POST.get('delete_id')
        case = get_object_or_404(PregnancyCase, pregnancycase_id=delete_id)
        if case.user_id == user.user_id:
            case.delete()
            messages.success(request, '已刪除胎數紀錄。')
        return redirect('pregnancy_case')

    cases_own = list(
        PregnancyCase.objects.filter(user=user).prefetch_related('babyinformation_set')
    )
    member_ids = _member_case_ids(user, request)
    shared = list(
        PregnancyCase.objects.filter(pregnancycase_id__in=member_ids)
        .prefetch_related('babyinformation_set')
    ) if member_ids else []
    cases_dict = {c.pregnancycase_id: c for c in cases_own + shared}
    cases_list = sorted(cases_dict.values(), key=lambda c: c.create_time)
    active_cases, born_babies = partition_pregnancy_cases(cases_list)


     # 標記哪些 case 是自己建的（模板用來決定是否顯示編輯/刪除）
    own_case_ids = {c.pregnancycase_id for c in cases_own}
    for case in active_cases:
        case.is_owner = case.pregnancycase_id in own_case_ids
    for baby in born_babies:
        baby.case_id_for_edit = baby._case_id
        baby.is_owner = baby.pregnancycase_id in own_case_ids


    return render(request, 'pregnancycase/pregnancycase.html', {
        'active_cases': active_cases,
        'born_babies': born_babies,
    })


def _build_add_form_state(post, baby_count):
    """把送出的表單內容整理成 JS 可回填的結構（驗證失敗重繪嬰幼兒卡片時使用）。"""
    babies = []
    for num in range(1, baby_count + 1):
        babies.append({
            'name': post.get(f'baby_name_{num}', '') or '',
            'gender': post.get(f'gender_{num}', '') or '',
            'birthdaytime': post.get(f'birthdaytime_{num}', '') or '',
            'weight': post.get(f'baby_weight_{num}', '') or '',
            'height': post.get(f'baby_height_{num}', '') or '',
            'head': post.get(f'baby_head_{num}', '') or '',
            'chest': post.get(f'baby_chest_{num}', '') or '',
            'production_method': post.get(f'production_method_{num}', '') or '',
        })
    return {
        'baby_count': post.get('baby_count', '1') or '1',
        'triplet_count': post.get('triplet_count', '3') or '3',
        'babies': babies,
    }


def add_pregnancy_case(request):
    if request.method == 'POST':
        user = get_current_user_profile(request)
        if not user:
            return redirect('login')
        menstruation_str = (request.POST.get('menstruation') or '').strip()
        expecteddate_str = (request.POST.get('expecteddate') or '').strip()
        code = (request.POST.get('code') or '').strip()

        # 解析胎兒數：1 / 2 / 3+（三胞胎時讀取 triplet_count）
        # 提前解析，驗證失敗重繪時才能把胎數與各寶寶欄位一起還原
        baby_count_raw = request.POST.get('baby_count', '1')
        if baby_count_raw == '3+':
            try:
                baby_count = max(3, min(8, int(request.POST.get('triplet_count', 3))))
            except (ValueError, TypeError):
                baby_count = 3
        else:
            try:
                baby_count = max(1, min(8, int(baby_count_raw)))
            except (ValueError, TypeError):
                baby_count = 1

        # 加入碼只在這裡決定一次：使用者送回來的若仍可用就沿用，
        # 否則才產生新的。驗證失敗重繪時不再每次換一組新碼。
        if not code or PregnancyCase.objects.filter(code=code).exists():
            code = _generate_unique_code()

        def _render_form(error_msg):
            return render(request, 'pregnancycase/add_pregnancy_case.html', {
                'generated_code': code,
                'error': error_msg,
                'form_data': request.POST,
                'form_state': _build_add_form_state(request.POST, baby_count),
            })

        # 最後月經日期／預產期擇一必填，缺少的一方由系統自動推算（Naegele's Rule）
        if not menstruation_str and not expecteddate_str:
            return _render_form('請填寫最後一次月經日期或預產期（擇一填寫即可）')

        menstruation = None
        if menstruation_str:
            try:
                menstruation = datetime.strptime(menstruation_str, '%Y-%m-%d').date()
            except ValueError:
                return _render_form('月經日期格式不正確，請重新輸入')

        expecteddate = None
        if expecteddate_str:
            try:
                expecteddate = datetime.strptime(expecteddate_str, '%Y-%m-%d').date()
            except ValueError:
                return _render_form('預產期格式不正確，請重新輸入')

        # 兩者擇一送出即可：缺少的那一個由另一個自動推算（Naegele's Rule／其反推公式）
        if menstruation and not expecteddate:
            expecteddate = _calculate_expected_date(menstruation)
        elif expecteddate and not menstruation:
            menstruation = _calculate_lmp_from_due(expecteddate)

        # ── 先解析並驗證每一位嬰幼兒的出生時間，全部通過才寫入資料庫 ──────
        # （避免像舊版一樣先建立 PregnancyCase，遇到驗證失敗又留下孤兒資料）
        babies_payload = []
        for i in range(baby_count):
            num = i + 1
            birthdaytime_str = (
                request.POST.get(f'birthdaytime_{num}')
                or request.POST.get('birthdaytime')
                or ''
            ).strip()
            birthdaytime = None
            if birthdaytime_str:
                try:
                    birthdaytime = timezone.make_aware(datetime.strptime(birthdaytime_str, '%Y-%m-%dT%H:%M'))
                except ValueError:
                    return _render_form(f'第 {num} 位嬰幼兒的出生時間格式不正確')

            birth_error = validate_birth_datetime(menstruation, birthdaytime)
            if birth_error:
                return _render_form(f'第 {num} 位嬰幼兒：{birth_error}')

            name = (request.POST.get(f'baby_name_{num}') or '').strip() or f'嬰幼兒 {num}'
            gender = (request.POST.get(f'gender_{num}') or '').strip()
            if gender not in {'1', '2'}:
                return _render_form(f'第 {num} 位嬰幼兒：請選擇性別')
            w  = _parse_float(request.POST.get(f'baby_weight_{num}') or request.POST.get('baby_weight'))
            h  = _parse_float(request.POST.get(f'baby_height_{num}') or request.POST.get('baby_height'))
            hc = _parse_float(request.POST.get(f'baby_head_{num}') or request.POST.get('baby_head'))
            cc = _parse_float(request.POST.get(f'baby_chest_{num}') or request.POST.get('baby_chest'))

            # ── 體徵範圍驗證（lazy import 避免與 baby_utils 循環相依） ──
            from views.baby_utils import validate_birth_vitals
            vital_error = validate_birth_vitals(w, h, hc, cc)
            if vital_error:
                return _render_form(f'第 {num} 位嬰幼兒體徵：{vital_error}')

            babies_payload.append({
                'name': name,
                'gender': gender,
                'birthdaytime': birthdaytime,
                'baby_height': h,
                'baby_weight': w,
                'babyheadcircumference': hc,
                'chestcircumference': cc,
                'production_method': request.POST.get(f'production_method_{num}') or request.POST.get('production_method'),
            })

        # ── 驗證全數通過，才真正建立 PregnancyCase 與嬰幼兒資料 ──────────
        case = PregnancyCase.objects.create(
            user=user,
            menstruation=menstruation,
            expecteddate=expecteddate,
            code=code,
            create_time=timezone.now(),
        )

        first_baby = None
        for payload in babies_payload:
            baby = BabyInformation.objects.create(pregnancycase=case, **payload)
            if first_baby is None:
                first_baby = baby

        # 切換 session 至第一位嬰幼兒（多胎時使用者可在嬰幼兒頁面自行切換）
        if first_baby:
            request.session['active_baby_id'] = first_baby.baby_id
            request.session.modified = True

        return redirect('pregnancy_case')

    user = get_current_user_profile(request)
    if not user:
        return redirect('login')
    code = _generate_unique_code()
    return render(request, 'pregnancycase/add_pregnancy_case.html', {'generated_code': code})

def edit_pregnancy_case(request):
    case_id = request.GET.get('id')
    if not case_id:
        return redirect('pregnancy_case')

    case = get_object_or_404(PregnancyCase, pregnancycase_id=case_id)
    current_user = get_current_user_profile(request)
    if not current_user or case.user_id != current_user.user_id:
        return redirect('login')

    # 取全部寶寶（多胎支援）
    babies = list(case.babyinformation_set.all().order_by('baby_id'))
    posted_data = request.POST if request.method == 'POST' else {}

    def _build_baby_inputs():
        rows = []
        if babies:
            for i, baby in enumerate(babies, start=1):
                raw_name = (posted_data.get(f'baby_name_{i}') or '').strip()
                raw_birthday = (posted_data.get(f'birthdaytime_{i}') or '').strip()
                raw_gender = (posted_data.get(f'gender_{i}') or '').strip()
                # 出生時間必須先轉成當地時間再輸出給 datetime-local，
                # 否則表單顯示的是 UTC，存回時又被 make_aware 成台北，每存一次就退 8 小時。
                local_birthday = _local_birth_datetime(baby.birthdaytime)
                rows.append({
                    'name': raw_name or baby.name,
                    # gender 必須回傳給模板，否則下拉沒有預選值，存檔後一律變「男」
                    'gender': raw_gender or (baby.gender or ''),
                    'birthdaytime': raw_birthday or (
                        local_birthday.strftime('%Y-%m-%dT%H:%M') if local_birthday else ''
                    ),
                })
        else:
            rows.append({
                'name': (posted_data.get('baby_name_1') or '').strip(),
                'gender': (posted_data.get('gender_1') or '').strip(),
                'birthdaytime': (posted_data.get('birthdaytime_1') or '').strip(),
            })
        return rows

    if request.method == 'POST':
        menstruation_str = (request.POST.get('menstruation') or '').strip()
        expecteddate_str = (request.POST.get('expecteddate') or '').strip()

        def _render_error(error_msg, menstruation_str_val, expecteddate_str_val):
            return render(request, 'pregnancycase/edit_pregnancy_case.html', {
                'case': case,
                'babies': babies,
                'baby_inputs': _build_baby_inputs(),
                'form_data': request.POST,
                'menstruation_str': menstruation_str_val,
                'expecteddate_str': expecteddate_str_val,
                'error': error_msg,
            })

        # 與新增頁一致：最後月經日期／預產期二擇一即可，缺少的一方自動推算
        if not menstruation_str and not expecteddate_str:
            return _render_error(
                '請填寫最後一次月經日期或預產期（擇一填寫即可）',
                case.menstruation.strftime('%Y-%m-%d') if case.menstruation else '',
                case.expecteddate.strftime('%Y-%m-%d') if case.expecteddate else '',
            )

        new_menstruation = None
        if menstruation_str:
            try:
                new_menstruation = datetime.strptime(menstruation_str, '%Y-%m-%d').date()
            except ValueError:
                return _render_error('月經日期格式不正確，請重新輸入', menstruation_str, expecteddate_str)

        try:
            new_expecteddate = datetime.strptime(expecteddate_str, '%Y-%m-%d').date() if expecteddate_str else None
        except ValueError:
            return _render_error('預產期格式不正確，請重新輸入', menstruation_str, expecteddate_str)

        # 只填預產期時，用 Naegele's Rule 反推最後月經日期
        if not new_menstruation:
            new_menstruation = _calculate_lmp_from_due(new_expecteddate)
            if not new_menstruation:
                return _render_error('預產期格式不正確，請重新輸入', menstruation_str, expecteddate_str)
        if not new_expecteddate:
            new_expecteddate = _calculate_expected_date(new_menstruation)

        # ── 逐一解析每位寶寶的姓名／出生時間，並用「新的 LMP」驗證合理性 ──────
        # 這裡是關鍵修正：LMP 是推算出生週數的權威基準，若使用者調整了 LMP，
        # 必須重新檢查所有既有（或本次修改）的出生時間是否仍落在 14w~43w 合理區間，
        # 否則會產生「出生時間與新 LMP 矛盾」的髒資料且完全無提示。
        baby_updates = []  # [(baby, new_name, new_gender, new_birthdaytime), ...]
        for i, baby in enumerate(babies, start=1):
            new_name = (request.POST.get(f'baby_name_{i}') or '').strip() or baby.name
            new_gender = (request.POST.get(f'gender_{i}') or '').strip()
            if new_gender not in {'1', '2'}:
                return _render_error(f'「{baby.name}」：請選擇性別', menstruation_str, expecteddate_str)

            birthdaytime_str = (request.POST.get(f'birthdaytime_{i}') or '').strip()
            if birthdaytime_str:
                try:
                    new_birthdaytime = timezone.make_aware(
                        datetime.strptime(birthdaytime_str, '%Y-%m-%dT%H:%M')
                    )
                except ValueError:
                    return _render_error(
                        f'「{baby.name}」的出生時間格式不正確', menstruation_str, expecteddate_str
                    )
            else:
                # 沒有送出新的出生時間 → 沿用原本的值，但仍要用新 LMP 重新驗證
                new_birthdaytime = baby.birthdaytime

            birth_error = validate_birth_datetime(new_menstruation, new_birthdaytime)
            if birth_error:
                return _render_error(f'「{baby.name}」：{birth_error}', menstruation_str, expecteddate_str)

            baby_updates.append((baby, new_name, new_gender, new_birthdaytime))

        # 若 case 下還沒有任何寶寶，且有填 baby_name_1，一併驗證後再建立
        new_baby_payload = None
        if not babies:
            new_name = (request.POST.get('baby_name_1') or '').strip()
            if new_name:
                birthdaytime_str = (request.POST.get('birthdaytime_1') or '').strip()
                new_birthdaytime = None
                if birthdaytime_str:
                    try:
                        new_birthdaytime = timezone.make_aware(
                            datetime.strptime(birthdaytime_str, '%Y-%m-%dT%H:%M')
                        )
                    except ValueError:
                        return _render_error('出生時間格式不正確', menstruation_str, expecteddate_str)
                birth_error = validate_birth_datetime(new_menstruation, new_birthdaytime)
                if birth_error:
                    return _render_error(birth_error, menstruation_str, expecteddate_str)
                gender = (request.POST.get('gender_1') or '').strip()
                if gender not in {'1', '2'}:
                    return _render_error('請選擇性別', menstruation_str, expecteddate_str)
                new_baby_payload = {
                    'name': new_name,
                    'gender': gender,
                    'birthdaytime': new_birthdaytime,
                }

        # ── 驗證全數通過，才真正落地寫入 ──────────────────────────────
        case.menstruation = new_menstruation
        case.expecteddate = new_expecteddate
        case.save()

        for baby, new_name, new_gender, new_birthdaytime in baby_updates:
            baby.name = new_name
            baby.gender = new_gender
            baby.birthdaytime = new_birthdaytime
            baby.save()

        if new_baby_payload:
            BabyInformation.objects.create(pregnancycase=case, **new_baby_payload)

        return redirect('pregnancy_case')

    status = get_pregnancy_status(case)
    return render(request, 'pregnancycase/edit_pregnancy_case.html', {
        'case': case,
        'babies': babies,
        'baby_inputs': _build_baby_inputs(),
        'form_data': request.POST if request.method == 'POST' else {},
        'menstruation_str': case.menstruation.strftime('%Y-%m-%d') if case.menstruation else '',
        'expecteddate_str': case.expecteddate.strftime('%Y-%m-%d') if case.expecteddate else '',
        'is_overdue': status == 'overdue',
    })
