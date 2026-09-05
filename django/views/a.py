from django.shortcuts import render
from views.pregnancycase import baby_switcher, sync_active_selection_from_request
from views.session_utils import get_current_user_profile


def ai_growth(request):
    user = get_current_user_profile(request)
    context = {'active_v2_tab': '1'}
    if user:
        sync_active_selection_from_request(request, user)
        context.update(baby_switcher(request))
    return render(request, "1/ai_growth.html", context)

def m(request):
    user = get_current_user_profile(request)
    context = {'active_v2_tab': 'm'}
    if user:
        sync_active_selection_from_request(request, user)
        context.update(baby_switcher(request))
    return render(request, "1/m.html", context)

def s(request):
    user = get_current_user_profile(request)
    context = {'active_v2_tab': 's'}
    if user:
        sync_active_selection_from_request(request, user)
        context.update(baby_switcher(request))
    return render(request, "1/s.html", context)


