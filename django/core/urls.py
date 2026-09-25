"""Core URL routes."""

from django.urls import path, include

from views import (
    qa,
    index,
    pregnancyrecordadd,
    userprofile,
    care_record,
    pregnancycase,
    login,
    edit_family_member,
    baby_home,
    baby_information,
    baby_record,
    baby_growthmap,
    ai_growth,
    social_sharing_card,
    history_review,
    history_review_v3,
	assistant,
	a
)


urlpatterns = [
    # ======================
    # 首頁
    # ======================
    path('', index.index, name='index'),


    # ======================
    # 登入 / 帳號
    # ======================
    path('login/', login.login_page, name='login'),

    path('accounts/', include('allauth.urls')),

    # 🔑 這條是給登入頁的 Google Identity Services（GSI）按鈕使用的 callback，
    # 只處理一般登入時 POST 過來的 credential JWT（見 login.google_auth_login）。
    #
    # 注意：name 絕對不能取成 'google_callback'！
    # allauth 內部會用 reverse('google_callback') 算出它自己 OAuth 流程要用的
    # redirect_uri（也就是 /accounts/google/login/callback/）。如果這裡撞名，
    # allauth 綁定帳號（process=connect）時算出來的網址會被這條路由劫持，
    # 導致 Google 導回時打到這裡卻用 GET 帶 code，而這裡只接受 POST，
    # 進而出現 405 或後續一連串連動錯誤。
    path('api/auth/callback', login.google_auth_login, name='gsi_google_callback'),

    path(
        'google_auth_login/',
        login.google_auth_login,
        name='google_auth_login'
    ),

    path(
        'google_auth_login',
        login.google_auth_login,
        name='google_auth_login_no_slash'
    ),

    path(
        'api/auth/google/',
        login.google_auth_login
    ),

    path(
        'logout/',
        login.logout_user,
        name='logout'
    ),

    # 帳號綁定（個人資料頁使用）
    path(
        'bind_google_account/',
        login.bind_google_account,
        name='bind_google_account'
    ),

    path(
        'bind_line_account/',
        login.bind_line_account,
        name='bind_line_account'
    ),

    # ======================
    # 育兒提醒
    # ======================
    path(
        'add_care_reminder/',
        care_record.add_care_reminder,
        name='add_care_reminder'
    ),

    path(
        'set_care_status/',
        care_record.set_care_status,
        name='set_care_status'
    ),

    path('delete_care_reminder/', care_record.delete_care_reminder, name='delete_care_reminder'),
    path('care-reminder/edit/', care_record.edit_care_reminder, name='edit_care_reminder'),
    # ======================
    # 孕期紀錄
    # ======================
    path(
        'pregnancyrecord/add/',
        pregnancyrecordadd.pregnancyrecord_add,
        name='pregnancy_record_add'
    ),

    path(
        'pregnancyrecord/',
        pregnancyrecordadd.pregnancyrecord,
        name='pregnancyrecord'
    ),

    path(
        'pregnancyrecord_new/',
        pregnancyrecordadd.pregnancyrecord_new,
        name='pregnancy_record_new'
    ),


    # ======================
    # 懷孕胎數
    # ======================
    path(
        'pregnancycase/',
        pregnancycase.pregnancy_case,
        name='pregnancy_case'
    ),

    path(
        'add_pregnancy_baby/',
        pregnancycase.add_pregnancy_case,
        name='add_pregnancy_case'
    ),

    path(
        'edit_pregnancy_case/',
        pregnancycase.edit_pregnancy_case,
        name='edit_pregnancy_case'
    ),


    # ======================
    # 嬰幼兒首頁
    # ======================
    path(
        'babyinformation/',
        baby_home.baby,
        name='babyinformation'
    ),


    # 嬰幼兒成長圖表
    path(
        'babygrowthmap/',
        baby_growthmap.baby_growthmap,
        name='babygrowthmap'
    ),


    # ======================
    # 嬰幼兒基本資料
    # ======================
    path(
        'add_baby_information/',
        baby_information.add_baby_information,
        name='add_baby_information'
    ),

    path(
        'edit_baby_information/',
        baby_information.edit_baby_information,
        name='edit_baby_information'
    ),

    path(
        'delete_baby_information/',
        baby_information.delete_baby_information,
        name='delete_baby_information'
    ),


    # ======================
    # 嬰幼兒成長紀錄
    # ======================
    path(
        'babyrecord/add/',
        baby_record.add_baby_record,
        name='add_baby_record'
    ),

    path(
        'babyrecord/edit/<int:babyrecord_id>/',
        baby_record.edit_baby_record,
        name='edit_baby_record'
    ),

    path(
        'babyrecord/<int:babyrecord_id>/delete/',
        baby_record.delete_baby_record,
        name='delete_baby_record'
    ),


    # ======================
    # AI 問答
    # ======================
    path(
        'qa/',
        qa.qa_conversation,
        name='qa_conversation'
    ),

    # 刪除 AI 問答對話（只接受 POST）
    path(
        'qa/delete/',
        qa.qa_delete_conversation,
        name='qa_delete_conversation'
    ),

    path(
        'assistant/',
        assistant.assistant,
        name='assistant'
    ),


    # ======================
    # AI 成長足跡
    # ======================
    path(
        'ai-growth/',
        ai_growth.ai_growth,
        name='ai_growth'
    ),


    # ======================
    # 社群分享卡片
    # ======================
    path(
        'social_sharing_card/',
        social_sharing_card.social_sharing_card_view,
        name='social_sharing_card'
    ),
    path(
        'api/upload_sharing_card/',
        social_sharing_card.upload_sharing_card,
        name='upload_sharing_card'
    ),
    path(
        'share_card/<str:filename>/',
        social_sharing_card.share_card_detail_view,
        name='share_card_detail'
    ),




    # ======================
    # 個人資料
    # ======================
    path(
        'userprofile/',
        userprofile.userprofile,
        name='profile'
    ),

    path(
        'edit_userprofile/',
        userprofile.edit_userprofile,
        name='edit_userprofile'
    ),

    path(
        'userprofile/update_profile/',
        userprofile.update_profile,
        name='update_profile'
    ),


    # ======================
    # 家庭照顧者
    # ======================
    path(
        'edit_family_member/',
        edit_family_member.edit_family_member,
        name='edit_family_member'
    ),

    path(
        'edit_helper_permissions/',
        edit_family_member.edit_helper_permissions,
        name='edit_helper_permissions'
    ),

    # 家庭成員的各項異動（一律 POST + CSRF，處理完 redirect 回原頁）
    path(
        'family/permissions/save/',
        edit_family_member.save_permissions,
        name='save_family_permissions'
    ),

    path(
        'family/request/handle/',
        edit_family_member.handle_join_request,
        name='handle_join_request'
    ),

    path(
        'family/request/cancel/',
        edit_family_member.cancel_join_request,
        name='cancel_join_request'
    ),

    path(
        'family/member/remove/',
        edit_family_member.remove_family_member,
        name='remove_family_member'
    ),

    path(
        'family/leave/',
        edit_family_member.leave_family,
        name='leave_family'
    ),

    path(
        'join_family/',
        userprofile.join_family,
        name='join_family'
    ),


    # ======================
    # 歷史回顧（第一版）
    # ======================
    path(
        'history-review/',
        history_review.history_review,
        name='history_review'
    ),
    path(
        'history-review/pregnancy-journey/',
        history_review.pregnancy_journey_view,
        name='history_pregnancy_journey'
    ),
    path(
        'history-review/baby-growth/',
        history_review.baby_growth_view,
        name='history_baby_growth'
    ),
    path(
        'history-review/memory-wall/',
        history_review.memory_wall_view,
        name='history_memory_wall'
    ),
    path(
        'history-review/ai-growth-journey/',
        history_review.ai_growth_journey_view,
        name='history_ai_growth_journey'
    ),
    path(
        'history-review/phase-review/',
        history_review.ai_growth_journey_view,
        name='history_phase_review'
    ),
    path(
        'history-review/share-card/',
        social_sharing_card.social_sharing_card_view,
        name='history_share_card'
    ),
    path(
        'history_review/',
        history_review.history_review,
        name='history_review_alias'
    ),

    # ======================
    # 歷史回顧（第三版）
    # ======================
    path(
        'history-review-v3/',
        history_review_v3.v3_timeline,
        name='history_review_v3'
    ),
    path(
        'history_review_v3/',
        history_review_v3.v3_timeline,
        name='history_review_v3_underscore_alias'
    ),
    path(
        'history-review-v3/memory-wall/',
        history_review_v3.v3_memory_wall,
        name='history_review_v3_memory_wall'
    ),
    path(
        'history_review_v3/memory_wall/',
        history_review_v3.v3_memory_wall,
        name='history_review_v3_memory_wall_alias'
    ),
    path(
        'history_review_v3/memory-wall/',
        history_review_v3.v3_memory_wall,
        name='history_review_v3_memory_wall_alias2'
    ),
    path(
        'history-review-v3/baby-growth/',
        history_review_v3.v3_baby_growth,
        name='history_review_v3_baby_growth'
    ),

		# ------------------------
	path('1/',a.ai_growth,name='1'),
	path('m/',a.m,name='m'),
	path('s/',a.s,name='s'),

]
