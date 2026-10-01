# baby_growthmap.py
from django.shortcuts import render, redirect
from django.urls import reverse
from views import baby_utils
from views.session_utils import get_current_user_profile
from views.pregnancycase import url_with_active_selection

def baby_growthmap(request):
    """成長里程碑地圖（獨立分頁）"""
    user = get_current_user_profile(request)
    if not user:
        return redirect('login')

    baby = baby_utils.get_active_baby(request)

    context = baby_utils.build_growth_timeline_context(baby)
    context['back_url'] = url_with_active_selection(request, reverse('babyinformation'))
    return render(request, "baby/baby_growthmap.html", context)