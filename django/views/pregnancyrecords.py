from datetime import timedelta
from core.models import PregnancyRecord, PregnancyCase


def case_date_window(pregnancy_case):
    """回傳這個個案的紀錄日期區間 (start, end)；None 代表該側不設限。

    最早的個案（或唯一的個案）起始日不設限，最後月經日以前的紀錄也會顯示。

    從 records_for_case 抽出來，讓清理舊資料的指令可以共用同一套區間判斷。
    """
    if not pregnancy_case or not pregnancy_case.user_id:
        return None, None

    cases = list(
        PregnancyCase.objects.filter(user_id=pregnancy_case.user_id)
        .order_by('menstruation', 'pregnancycase_id')
    )
    # 最後月經日以前的紀錄（例如備孕期就開始使用系統）也要顯示，
    # 所以只有一個個案時，起始日不設限。
    if len(cases) <= 1:
        return None, None

    try:
        idx = [c.pregnancycase_id for c in cases].index(pregnancy_case.pregnancycase_id)
    except ValueError:
        return None, None

    case = cases[idx]
    # 第一個個案：起始日不設限，備孕期的紀錄也算在內。
    # 之後的個案：以自己的最後月經日為起點，避免和前一胎的紀錄混在一起。
    start_date = None if idx == 0 else case.menstruation

    born_babies = (
        case.babyinformation_set
        .filter(birthdaytime__isnull=False)
        .order_by('-birthdaytime')
    )
    if born_babies.exists():
        birth_date = born_babies.first().birthdaytime.date()
        if idx + 1 < len(cases):
            next_case = cases[idx + 1]
            end_date = min(birth_date + timedelta(days=60), next_case.menstruation - timedelta(days=1))
        else:
            end_date = birth_date + timedelta(days=60)
    else:
        if idx + 1 < len(cases):
            next_case = cases[idx + 1]
            end_date = next_case.menstruation - timedelta(days=1)
        else:
            end_date = None

    return start_date, end_date


def records_for_case(pregnancy_case):
    if not pregnancy_case or not pregnancy_case.user_id:
        return PregnancyRecord.objects.none()

    start_date, end_date = case_date_window(pregnancy_case)

    qs = PregnancyRecord.objects.filter(user_id=pregnancy_case.user_id)
    if start_date:
        qs = qs.filter(check_date__gte=start_date)
    if end_date:
        qs = qs.filter(check_date__lte=end_date)
    return qs