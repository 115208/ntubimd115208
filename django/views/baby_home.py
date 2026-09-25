import datetime
import calendar

from django.shortcuts import render, redirect
from django.utils import timezone

from core.models import BabyRecord, BabyGrowthMap, BabyStatus, FamilyMember
from views import baby_utils
from views.pregnancycase import get_pregnancy_status
from views.session_utils import get_current_user_profile


def _fill_forward_growth_data(records):
    sorted_records = sorted(
        records,
        key=lambda r: (r.date.date() if hasattr(r.date, 'date') else r.date),
    )

    last_height = last_weight = last_head = last_chest = None
    result = []

    for rec in sorted_records:
        filled_height = rec.height if rec.height is not None else last_height
        filled_weight = rec.weight if rec.weight is not None else last_weight
        filled_head = rec.headcircumference if rec.headcircumference is not None else last_head
        filled_chest = rec.chestcircumference if rec.chestcircumference is not None else last_chest

        result.append({
            'record': rec,
            'date': rec.date.date() if hasattr(rec.date, 'date') else rec.date,
            'height': filled_height,
            'weight': filled_weight,
            'headcircumference': filled_head,
            'chestcircumference': filled_chest,
            'is_carried_height': (rec.height is None and filled_height is not None),
            'is_carried_weight': (rec.weight is None and filled_weight is not None),
            'is_carried_head': (rec.headcircumference is None and filled_head is not None),
            'is_carried_chest': (rec.chestcircumference is None and filled_chest is not None),
        })

        if rec.height is not None:
            last_height = rec.height
        if rec.weight is not None:
            last_weight = rec.weight
        if rec.headcircumference is not None:
            last_head = rec.headcircumference
        if rec.chestcircumference is not None:
            last_chest = rec.chestcircumference

    return result


def _get_calendar_data(records, selected_date):
    year, month = selected_date.year, selected_date.month
    first_weekday, days_in_month = calendar.monthrange(year, month)

    # 1. 取得今天日期（Asia/Taipei 當地日），用來比對是否為未來日期
    today = timezone.localdate()

    # 填補當月第一天之前的空白格子
    cells = [{'empty': True} for _ in range((first_weekday + 1) % 7)]

    # 整理當月已有紀錄的日期
    record_days = {
        (r.date.date().day if hasattr(r.date, 'date') else r.date.day): r
        for r in records
        if (r.date.date().year if hasattr(r.date, 'date') else r.date.year) == year
        and (r.date.date().month if hasattr(r.date, 'date') else r.date.month) == month
    }

    # 生成每一天的日期格子
    for day in range(1, days_in_month + 1):
        d = datetime.date(year, month, day)
        cells.append({
            'empty': False,
            'day': day,
            'date_iso': d.isoformat(),
            'is_selected': d == selected_date,
            'has_record': day in record_days,
            'is_future': d > today,  # 👈 核心修改：判斷是否大於今天
        })

    # 填補當月最後一天之後的空白格子，確保每週 7 格
    while len(cells) % 7 != 0:
        cells.append({'empty': True})

    calendar_weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]
    while len(calendar_weeks) < 5:
        calendar_weeks.append([{'empty': True} for _ in range(7)])

    record_years = {today.year, year} | {
        (r.date.date().year if isinstance(r.date, datetime.datetime) else r.date.year)
        for r in records
    }

    return {
        'calendar_weeks': calendar_weeks,
        'selected_year': year,
        'selected_month': month,
        'selected_month_label': f'{year}年 {month}月',
        'selected_date_iso': selected_date.isoformat(),
        'selected_day': selected_date.day,
        'calendar_years': sorted(record_years, reverse=True),
        'calendar_months': list(range(1, 13)),
    }


def _get_baby_milestones_summary(baby):
    if not baby:
        return []

    growth_maps = BabyGrowthMap.objects.all().order_by('timecourse')
    baby_records = list(BabyRecord.objects.filter(baby=baby))
    achieved_map = {
        bs.babygrowthmap_id: bs
        for bs in BabyStatus.objects.filter(babyrecord__baby=baby)
        .select_related('babygrowthmap', 'babyrecord')
    }

    # 效能：舊版在「里程碑 × 紀錄」的巢狀迴圈內對每一筆紀錄重查一次 DB
    #（最壞情況數千次查詢）。這裡先一次把每筆紀錄的里程碑撈好放進字典。
    milestones_by_record = {
        rec.babyrecord_id: baby_utils.split_note_and_milestones(rec)[0]
        for rec in baby_records
    }

    completed_list = []

    for idx, growth_map in enumerate(growth_maps):
        bs = achieved_map.get(growth_map.pk)
        is_completed, matching_record = bs is not None, bs.babyrecord if bs else None

        if not is_completed:
            for rec in baby_records:
                if growth_map.growthrecord in milestones_by_record.get(rec.babyrecord_id, []):
                    is_completed, matching_record = True, rec
                    break

        if is_completed and matching_record:
            completed_list.append({
                'growthrecord': growth_map.growthrecord,
                'timecourse': growth_map.timecourse,
                'status': 'completed',
                'achieved_date': (
                    matching_record.date.strftime('%Y.%m.%d')
                    if hasattr(matching_record.date, 'strftime')
                    else str(matching_record.date)
                ),
                'description': '',
                'sort_order': idx,
            })

    return sorted(completed_list, key=lambda x: x['sort_order'])


def baby(request):
    """主頁總覽"""
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    active_baby = baby_utils.get_active_baby(request)

    can_edit_baby = True
    can_use_assistant = True
    if active_baby and active_baby.pregnancycase and active_baby.pregnancycase.user_id != user.user_id:
        membership = FamilyMember.objects.filter(
            pregnancycase=active_baby.pregnancycase, user=user
        ).first()
        can_edit_baby = baby_utils.has_permission(membership, 'baby_records', 'edit')
        can_use_assistant = baby_utils.has_permission(membership, 'growth_assistant', 'view')


    records = (
        list(BabyRecord.objects.filter(baby=active_baby).order_by('-date'))
        if active_baby else []
    )

    for record in records:
        record.milestones, record.note_text = baby_utils.split_note_and_milestones(record)

    raw_date = request.GET.get('date', '')
    today = timezone.localdate()
    try:
        selected_date = (
            datetime.date.fromisoformat(raw_date)
            if raw_date else today
        )
    except Exception:
        selected_date = today

    if not raw_date and records:
        has_today = any((r.date.date() if hasattr(r.date, 'date') else r.date) == today for r in records)
        if not has_today:
            latest_r = records[0]
            selected_date = latest_r.date.date() if hasattr(latest_r.date, 'date') else latest_r.date

    filled_records = _fill_forward_growth_data(records)
    filled_by_date = {item['date']: item for item in filled_records}
    selected_day_records = [
        r for r in records
        if (r.date.date() if hasattr(r.date, 'date') else r.date) == selected_date
    ]
    selected_day_record = None

    if selected_day_records:
        primary = selected_day_records[0]
        merged_h = primary.height
        merged_w = primary.weight
        merged_hd = primary.headcircumference
        merged_ch = primary.chestcircumference
        all_m, all_n = [], []

        for r in selected_day_records:
            if merged_h is None:
                merged_h = r.height
            if merged_w is None:
                merged_w = r.weight
            if merged_hd is None:
                merged_hd = r.headcircumference
            if merged_ch is None:
                merged_ch = r.chestcircumference

            for ms in (r.milestones or []):
                if ms not in all_m:
                    all_m.append(ms)

            note = (r.note_text or '').strip()
            if note and note not in all_n:
                all_n.append(note)

        primary.height = merged_h
        primary.weight = merged_w
        primary.headcircumference = merged_hd
        primary.chestcircumference = merged_ch
        primary.milestones = all_m
        primary.note_text = '\n'.join(all_n)
        selected_day_record = primary

        filled = filled_by_date.get(selected_date)
        if filled and selected_day_record:
            if selected_day_record.height is None and filled['height'] is not None:
                selected_day_record.height = filled['height']
                selected_day_record.height_carried = True
            if selected_day_record.weight is None and filled['weight'] is not None:
                selected_day_record.weight = filled['weight']
                selected_day_record.weight_carried = True
            if selected_day_record.headcircumference is None and filled['headcircumference'] is not None:
                selected_day_record.headcircumference = filled['headcircumference']
                selected_day_record.head_carried = True
            if selected_day_record.chestcircumference is None and filled['chestcircumference'] is not None:
                selected_day_record.chestcircumference = filled['chestcircumference']
                selected_day_record.chest_carried = True

    if active_baby:
        summary = {
            'baby_name': active_baby.name or '小寶',
            'birth_week': baby_utils.get_birth_week(active_baby) or '-',
            'birth_method': active_baby.production_method or '-',
            'birth_time': (
                active_baby.birthdaytime.strftime('%Y.%m.%d %H:%M')
                if active_baby.birthdaytime else '-'
            ),
            'birth_height': active_baby.baby_height or '-',
            'birth_weight': active_baby.baby_weight or '-',
            'birth_head': active_baby.babyheadcircumference or '-',
            'birth_chest': active_baby.chestcircumference or '-',
        }
        baby_form = {
            'baby_name': active_baby.name or '',
            'birthdaytime_value': (
                active_baby.birthdaytime.strftime('%Y-%m-%dT%H:%M')
                if active_baby.birthdaytime else ''
            ),
            'birth_week': baby_utils.get_birth_week(active_baby) or '',
            'birth_weight': active_baby.baby_weight or '',
            'birth_height': active_baby.baby_height or '',
            'birth_head': active_baby.babyheadcircumference or '',
            'birth_chest': active_baby.chestcircumference or '',
            'production_method': active_baby.production_method or '',
            'join_code': (
                getattr(active_baby.pregnancycase, 'code', '')
                if active_baby.pregnancycase_id else ''
            ),
        }
    else:
        summary = {
            k: '-' for k in [
                'baby_name', 'birth_week', 'birth_method', 'birth_time',
                'birth_height', 'birth_weight', 'birth_head', 'birth_chest',
            ]
        }
        baby_form = {
            k: '' for k in [
                'baby_name', 'birthdaytime_value', 'birth_week', 'birth_weight',
                'birth_height', 'birth_head', 'birth_chest', 'production_method',
                'join_code',
            ]
        }

    is_overdue = False
    if active_baby and not active_baby.birthdaytime and active_baby.pregnancycase:
        is_overdue = get_pregnancy_status(active_baby.pregnancycase) == 'overdue'

    context = {
        'is_overdue': is_overdue,
        'baby': active_baby,
        'baby_is_born': bool(active_baby and active_baby.birthdaytime),
        'records': records,
        'baby_summary': summary,
        'baby_form': baby_form,
        'selected_date': selected_date,
        'selected_day_record': selected_day_record,
        'has_day_data': bool(selected_day_record),
        'milestones_summary': _get_baby_milestones_summary(active_baby),
        'can_edit_baby': can_edit_baby,
        'can_use_assistant': can_use_assistant,
        # 新增紀錄時若當天已有紀錄會改為更新既有紀錄，導回本頁後提示使用者
        'record_merged': request.GET.get('record_merged') == '1',
    }
    context.update(_get_calendar_data(records, selected_date))

    return render(request, 'baby/babyinformation.html', context)