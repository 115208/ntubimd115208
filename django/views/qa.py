import json
import os
import re
import socket
from urllib import error, request as urlrequest
from urllib.parse import urlparse
import logging
import time

from django.conf import settings
from django.contrib import messages as django_messages
from django.db.models import Max
from django.db import transaction
from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.utils import timezone
from django.urls import reverse
from views.session_utils import get_current_user_profile
from views.health_safety import (
    MEDICAL_DISCLAIMER,
    check_rate_limit,
    prepend_emergency_notice,
    validate_question_length,
)
from core.models import QAConversation, QAMessage

logger = logging.getLogger(__name__)


ACTIVE_CONVERSATION_SESSION_KEY = "qa_active_conversation_id"
RATE_LIMIT_SESSION_KEY = "qa_rate_limit_timestamps"

# 送給 n8n 的對話上下文（history）：最多幾則、總長度上限
HISTORY_MESSAGE_LIMIT = 6
HISTORY_MAX_CHARS = 2000

# DEFAULT_N8N_RAG_WEBHOOK_URL = "http://localhost:5678/webhook/b2489eda-0b01-425d-be17-3c817fb4cdcd"
DEFAULT_N8N_RAG_WEBHOOK_URL = "https://kathy1023.app.n8n.cloud/webhook/b2489eda-0b01-425d-be17-3c817fb4cdcd"

EXPECTED_N8N_RAG_WEBHOOK_PATH = "/webhook/b2489eda-0b01-425d-be17-3c817fb4cdcd"

# 對使用者顯示的通用錯誤訊息（細節只寫 logger，不回傳前端）
GENERIC_AI_ERROR_MESSAGE = "AI 目前無法回覆，請稍後再試。"


def _is_ajax(request):
    """統一的 AJAX 判斷：X-Requested-With 或 Accept 內含 application/json。"""
    accept_header = request.META.get("HTTP_ACCEPT", "") or ""
    try:
        if hasattr(request, "headers"):
            accept_header = request.headers.get("Accept", "") or accept_header
    except Exception:
        pass

    return (
        request.META.get("HTTP_X_REQUESTED_WITH") == "XMLHttpRequest"
        or "application/json" in accept_header
    )


def _public_debug(debug_info):
    """只有在 DEBUG 模式才把 n8n 原始回應等細節送到前端。"""
    if settings.DEBUG:
        return debug_info
    return None


def _normalize_webhook_url(webhook_url):
    webhook_url = str(webhook_url or "").strip()
    if not webhook_url:
        return ""

    if "://" not in webhook_url:
        webhook_url = f"http://{webhook_url}"

    return webhook_url


def _host_is_resolvable(webhook_url):
    parsed = urlparse(webhook_url)
    host = parsed.hostname
    if not host:
        return False

    try:
        socket.getaddrinfo(host, parsed.port or 80)
        return True
    except OSError:
        return False


def _has_expected_webhook_path(webhook_url):
    parsed = urlparse(webhook_url)
    return parsed.path.rstrip("/") == EXPECTED_N8N_RAG_WEBHOOK_PATH


def _get_webhook_url():
    webhook_url = _normalize_webhook_url(os.getenv("N8N_RAG_WEBHOOK_URL", ""))
    if webhook_url and _host_is_resolvable(webhook_url) and _has_expected_webhook_path(webhook_url):
        return webhook_url

    return DEFAULT_N8N_RAG_WEBHOOK_URL


def _get_timeout_seconds():
    raw_timeout = os.getenv("N8N_RAG_TIMEOUT_SECONDS", "60").strip()
    try:
        return max(5, int(raw_timeout))
    except ValueError:
        return 60


def _stringify_source(source_item):
    if isinstance(source_item, str):
        return source_item.strip()

    if isinstance(source_item, dict):
        title = source_item.get("title") or source_item.get("source") or source_item.get("name")
        page = source_item.get("page") or source_item.get("page_number")
        excerpt = source_item.get("excerpt") or source_item.get("content") or source_item.get("text")

        parts = []
        if title:
            parts.append(str(title))
        if page is not None and str(page).strip():
            parts.append(f"第 {page} 頁")
        if excerpt:
            parts.append(str(excerpt).strip())

        if parts:
            return "｜".join(parts)

        return json.dumps(source_item, ensure_ascii=False)

    return str(source_item).strip()


def _strip_markdown_emphasis(text):
    if not text:
        return ""

    normalized_text = str(text).strip()
    normalized_text = re.sub(r"\*\*(.+?)\*\*", r"\1", normalized_text, flags=re.S)
    normalized_text = re.sub(r"__(.+?)__", r"\1", normalized_text, flags=re.S)
    return normalized_text


def _extract_response_data(raw_body):
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"answer": raw_body, "sources": [], "raw": raw_body}

    if isinstance(data, list):
        data = data[0] if data else {}

    if not isinstance(data, dict):
        return {"answer": raw_body, "sources": [], "raw": data}

    answer = data.get("answer") or data.get("text") or data.get("output") or data.get("response") or ""
    if not answer and len(data) == 1:
        only_value = next(iter(data.values()))
        if isinstance(only_value, (str, int, float, bool)):
            answer = only_value

    if not answer:
        answer = json.dumps(data, ensure_ascii=False)

    sources = data.get("sources") or data.get("context") or data.get("documents") or []
    if isinstance(sources, dict):
        sources = [sources]
    elif isinstance(sources, str):
        sources = [sources]
    elif not isinstance(sources, list):
        sources = [sources] if sources else []

    normalized_sources = []
    for source_item in sources:
        source_text = _stringify_source(source_item)
        if source_text:
            normalized_sources.append(source_text)

    return {"answer": str(answer).strip(), "sources": normalized_sources, "raw": data}


def _post_to_n8n(question_text, history=None):
    webhook_url = _get_webhook_url()
    debug_info = {
        "request_url": webhook_url,
        "request_payload": (question_text or "")[:1000],
        "response_status": None,
        "response_body": None,
        "exception": None,
        "duration_seconds": None,
        # 失敗時建議回給前端的 HTTP 狀態碼
        "error_status": 502,
    }

    if not webhook_url:
        debug_info["exception"] = "missing_webhook_url"
        debug_info["error_status"] = 502
        return None, "請先設定 N8N_RAG_WEBHOOK_URL，讓 Django 可以呼叫 n8n Webhook。", debug_info

    logger.info("Posting to n8n webhook: %s", webhook_url)
    logger.debug("Payload question (truncated): %s", (question_text or '')[:200])
    # message / question 為 n8n 目前依賴的欄位，不可改名；history 為新增的上下文欄位。
    payload = json.dumps(
        {
            "message": question_text,
            "question": question_text,
            "history": history or [],
        }
    ).encode("utf-8")
    req = urlrequest.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    start = time.time()
    try:
        with urlrequest.urlopen(req, timeout=_get_timeout_seconds()) as response:
            raw_body = response.read().decode("utf-8", errors="ignore").strip()
            debug_info["response_status"] = getattr(response, 'status', None) or getattr(response, 'getcode', lambda: None)()
            debug_info["response_body"] = raw_body
            debug_info["duration_seconds"] = round(time.time() - start, 3)

            if not raw_body:
                return None, "n8n 回傳了空結果。", debug_info

            data = _extract_response_data(raw_body)
            return data, None, debug_info
    except error.HTTPError as exc:
        try:
            body = exc.read().decode('utf-8', errors='ignore')
        except Exception:
            body = ''
        debug_info["response_status"] = getattr(exc, 'code', None)
        debug_info["response_body"] = body
        debug_info["exception"] = f"HTTPError: {exc.reason}"
        debug_info["duration_seconds"] = round(time.time() - start, 3)
        debug_info["error_status"] = 504 if getattr(exc, 'code', None) in (408, 504) else 502
        logger.warning("n8n HTTPError %s %s: %s", exc.code, exc.reason, body)
        return None, f"n8n Webhook 回應失敗：{exc.code} {exc.reason}", debug_info
    except error.URLError as exc:
        debug_info["exception"] = f"URLError: {exc.reason}"
        debug_info["duration_seconds"] = round(time.time() - start, 3)
        debug_info["error_status"] = 504 if isinstance(getattr(exc, 'reason', None), (TimeoutError, socket.timeout)) else 502
        logger.warning("n8n URLError: %s", exc.reason)
        return None, f"無法連線到 n8n Webhook：{exc.reason}", debug_info
    except (TimeoutError, socket.timeout) as exc:
        debug_info["exception"] = f"Timeout: {exc}"
        debug_info["duration_seconds"] = round(time.time() - start, 3)
        debug_info["error_status"] = 504
        logger.warning("n8n timeout after %ss", debug_info["duration_seconds"])
        return None, "n8n 回應逾時。", debug_info
    except Exception as exc:
        debug_info["exception"] = str(exc)
        debug_info["duration_seconds"] = round(time.time() - start, 3)
        debug_info["error_status"] = 502
        logger.exception("Unexpected error while posting to n8n")
        return None, f"發生未知錯誤：{str(exc)}", debug_info


def _store_exchange(question_text, answer_text, user):
    """建立新對話並一次寫入 user / assistant 兩則訊息。使用者必須由呼叫端傳入。"""
    if not question_text or not answer_text:
        return None, "缺少問題或回答內容"

    if user is None:
        return None, "找不到登入中的使用者，無法寫入對話紀錄"

    try:
        now = timezone.now()
        conversation_title = (question_text[:50] or "孕期知識問答")
        with transaction.atomic():
            conversation = QAConversation.objects.create(
                user_id=user,
                title=conversation_title,
                create_time=now,
            )
            QAMessage.objects.create(
                qa_conversation=conversation,
                role="user",
                message=question_text,
                create_time=now,
            )
            QAMessage.objects.create(
                qa_conversation=conversation,
                role="assistant",
                message=answer_text,
                create_time=now,
            )
        return conversation, None
    except Exception as exc:
        logger.exception("Failed to store QA exchange")
        return None, str(exc)


def _append_exchange(conversation, question_text, answer_text):
    """把 user / assistant 兩則訊息寫進同一個 transaction，避免只寫進一半。"""
    if not conversation or not question_text or not answer_text:
        return "缺少對話或訊息內容"

    try:
        now = timezone.now()
        with transaction.atomic():
            QAMessage.objects.create(
                qa_conversation=conversation,
                role="user",
                message=question_text,
                create_time=now,
            )
            QAMessage.objects.create(
                qa_conversation=conversation,
                role="assistant",
                message=answer_text,
                create_time=now,
            )
        return None
    except Exception as exc:
        logger.exception("Failed to append QA messages")
        return str(exc)


def _get_conversation_by_id(conversation_id, user):
    """一定要帶 user，並以 user_id 過濾，避免讀到別人的對話。"""
    if not conversation_id or user is None:
        return None

    try:
        conversation_id = int(conversation_id)
    except (TypeError, ValueError):
        return None

    return QAConversation.objects.filter(
        qaconversation_id=conversation_id,
        user_id=user,
    ).first()


def _get_requested_conversation(request, user):
    """依請求解析目前對話，並驗證歸屬。

    回傳 (conversation, is_forbidden)：
    - 前端有明確帶 conversation_id 但查不到（不存在或不屬於本人）→ is_forbidden=True
    - 沒帶時才退回 session 記錄的對話
    """
    requested_id = request.POST.get("conversation_id") or request.GET.get("conversation_id")
    if requested_id:
        conversation = _get_conversation_by_id(requested_id, user)
        if conversation is None:
            logger.warning(
                "User %s requested QA conversation %s that is not theirs or does not exist",
                getattr(user, "user_id", None),
                requested_id,
            )
            return None, True
        return conversation, False

    session_conversation_id = request.session.get(ACTIVE_CONVERSATION_SESSION_KEY)
    return _get_conversation_by_id(session_conversation_id, user), False


def _set_current_conversation_id(request, conversation_id):
    if conversation_id:
        request.session[ACTIVE_CONVERSATION_SESSION_KEY] = str(conversation_id)
    else:
        request.session.pop(ACTIVE_CONVERSATION_SESSION_KEY, None)


def _load_conversation_messages(conversation):
    if not conversation:
        return []

    try:
        message_rows = list(conversation.messages.order_by("create_time", "serno").all())
    except Exception:
        logger.exception("Failed to load QA messages")
        return []

    return [
        {
            "role": message_row.role,
            "message": message_row.message,
            "create_time": message_row.create_time,
            "is_user": str(message_row.role).lower() == "user",
        }
        for message_row in message_rows
    ]


def _build_history_payload(conversation, limit=HISTORY_MESSAGE_LIMIT, max_chars=HISTORY_MAX_CHARS):
    """取該對話最近數則訊息當作 n8n 的上下文（含總長度上限）。"""
    if not conversation:
        return []

    message_rows = _load_conversation_messages(conversation)
    if not message_rows:
        return []

    history = []
    total_chars = 0
    for message_row in reversed(message_rows[-limit:]):
        text = str(message_row.get("message") or "").strip()
        if not text:
            continue
        if total_chars + len(text) > max_chars:
            break
        total_chars += len(text)
        history.append(
            {
                "role": "user" if message_row["is_user"] else "assistant",
                "message": text,
            }
        )

    history.reverse()
    return history


def _build_conversation_item(conversation, active_conversation_id=None):
    message_rows = _load_conversation_messages(conversation)
    user_messages = [message_row for message_row in message_rows if message_row["is_user"]]
    assistant_messages = [message_row for message_row in message_rows if not message_row["is_user"]]

    latest_message_time = message_rows[-1]["create_time"] if message_rows else None

    return {
        "id": conversation.qaconversation_id,
        "title": conversation.title,
        "create_time": conversation.create_time,
        "latest_message_time": latest_message_time,
        "question": user_messages[-1]["message"] if user_messages else conversation.title,
        "answers": [message_row["message"] for message_row in assistant_messages],
        "messages": message_rows,
        "is_active": str(conversation.qaconversation_id) == str(active_conversation_id),
    }


def _load_recent_items(limit=20, user=None):
    items = []

    if user is None:
        return items

    try:
        conversations = list(
            QAConversation.objects.annotate(latest_message_time=Max("messages__create_time"))
            .filter(user_id=user)
            .order_by("-latest_message_time", "-create_time")[:limit]
        )
    except Exception:
        logger.exception("Failed to load recent QA conversations")
        return items

    for conversation in conversations:
        item = _build_conversation_item(conversation)
        if not item["messages"]:
            continue

        items.append(item)

    return items


def _normalize_answer_text(answer_text, sources=None):
    if answer_text:
        normalized = _strip_markdown_emphasis(answer_text)
        if normalized:
            return normalized

    if sources:
        return "目前知識庫沒有直接對應的說明，以下是相關的參考資料來源。"

    return "目前資料庫沒有此資訊"


def _safe_avatar_url(user):
    """avatar 可能是 `default-avatar:<id>` 這類佔位字串，這種情況不要當圖片用。"""
    avatar = str(getattr(user, "avatar", "") or "").strip()
    if not avatar or avatar.startswith("default-avatar:"):
        return ""
    return avatar


def _validate_question(request, question):
    """長度上限與速率限制，回傳 (錯誤訊息, HTTP 狀態碼)；通過時回傳 (None, None)。"""
    length_error = validate_question_length(question)
    if length_error:
        return length_error, 400

    rate_error = check_rate_limit(request, RATE_LIMIT_SESSION_KEY)
    if rate_error:
        logger.info("QA rate limit hit for user session")
        return rate_error, 429

    return None, None


def qa_conversation(request):
    error_message = ""
    sources = []
    current_user = get_current_user_profile(request)
    is_ajax = _is_ajax(request)

    if not current_user:
        if is_ajax:
            return JsonResponse({"ok": False, "error": "登入狀態已失效或尚未登入，請重新登入後再送出訊息。"}, status=401)
        return redirect('login')

    # footer 的 /qa/?new=true 改在 server 端處理，避免先載入舊對話再由 JS 清空
    if request.method == "GET" and request.GET.get("new") == "true":
        _set_current_conversation_id(request, None)
        request.session["qa_skip_auto_load"] = True
        request.session.modified = True
        return redirect(reverse("qa_conversation"))

    if request.method == "POST" and request.POST.get("action") == "new_conversation":
        _set_current_conversation_id(request, None)
        request.session["qa_skip_auto_load"] = True
        request.session.modified = True
        if is_ajax:
            return JsonResponse(
                {
                    "ok": True,
                    "conversation_id": None,
                    "redirect_url": reverse("qa_conversation"),
                    "items": _load_recent_items(user=current_user),
                }
            )

        return redirect(reverse("qa_conversation"))

    if request.method == "POST":
        question = request.POST.get("question", "").strip()

        if not question:
            # AJAX 一律回 JSON，不要落到 render 整頁 HTML（前端會把 HTML 當成 AI 回答）
            if is_ajax:
                return JsonResponse({"ok": False, "error": "請輸入問題。"}, status=400)
            error_message = "請輸入問題。"
        else:
            reject_message, reject_status = _validate_question(request, question)
            if reject_message:
                if is_ajax:
                    return JsonResponse({"ok": False, "error": reject_message}, status=reject_status)
                error_message = reject_message
            else:
                conversation, is_forbidden = _get_requested_conversation(request, current_user)
                if is_forbidden:
                    if is_ajax:
                        return JsonResponse({"ok": False, "error": "找不到這個對話，或它不屬於你。"}, status=403)
                    django_messages.error(request, "找不到這個對話，或它不屬於你。")
                    return redirect(reverse("qa_conversation"))

                history = _build_history_payload(conversation)
                payload, n8n_error, debug_info = _post_to_n8n(question, history=history)
                store_error = None

                if payload:
                    sources = payload.get("sources") or []
                    answer = _normalize_answer_text(payload.get("answer") or "", sources)
                    answer, red_flags = prepend_emergency_notice(answer, question)
                    if red_flags:
                        logger.info("QA red flag keywords matched: %s", red_flags)

                    if answer:
                        if conversation:
                            store_error = _append_exchange(conversation, question, answer)
                            if store_error:
                                conversation = None
                        else:
                            conversation, store_error = _store_exchange(question, answer, current_user)

                        if conversation:
                            _set_current_conversation_id(request, conversation.qaconversation_id)
                            request.session.modified = True
                            if is_ajax:
                                return JsonResponse(
                                    {
                                        "ok": True,
                                        "conversation_id": conversation.qaconversation_id,
                                        "answer": answer,
                                        "sources": sources,
                                        "debug": _public_debug(debug_info),
                                    }
                                )
                            request.session["qa_last_debug"] = _public_debug(debug_info)
                            return redirect(
                                f"{reverse('qa_conversation')}?conversation_id={conversation.qaconversation_id}"
                            )

                # 走到這裡代表 n8n 失敗、回傳空結果，或寫入資料庫失敗
                if store_error:
                    debug_info["store_error"] = store_error
                    debug_info["error_status"] = 500
                    logger.error("Failed to persist QA exchange: %s", store_error)
                    friendly_error = "已取得回覆，但寫入對話紀錄失敗，請稍後再試。"
                else:
                    logger.warning(
                        "QA n8n failure: %s | status=%s exception=%s",
                        n8n_error,
                        debug_info.get("response_status"),
                        debug_info.get("exception"),
                    )
                    friendly_error = (
                        (n8n_error or GENERIC_AI_ERROR_MESSAGE)
                        if settings.DEBUG
                        else GENERIC_AI_ERROR_MESSAGE
                    )

                failure_status = debug_info.get("error_status") or 502
                if is_ajax:
                    return JsonResponse(
                        {
                            "ok": False,
                            "error": friendly_error,
                            "debug": _public_debug(debug_info),
                        },
                        status=failure_status,
                    )

                error_message = friendly_error
                request.session["qa_last_debug"] = _public_debug(debug_info)

    current_conversation, is_forbidden = _get_requested_conversation(request, current_user)
    if is_forbidden:
        django_messages.error(request, "找不到這個對話，或它不屬於你。")
        return redirect(reverse("qa_conversation"))

    skip_auto_load = request.session.pop("qa_skip_auto_load", False)

    if not current_conversation and not skip_auto_load:
        try:
            current_conversation = (
                QAConversation.objects.annotate(latest_message_time=Max("messages__create_time"))
                .filter(user_id=current_user)
                .order_by("-latest_message_time", "-create_time")
                .first()
            )
        except Exception:
            logger.exception("Failed to load latest QA conversation")
            current_conversation = None

    if current_conversation:
        _set_current_conversation_id(request, current_conversation.qaconversation_id)
    else:
        _set_current_conversation_id(request, None)

    active_conversation_id = request.session.get(ACTIVE_CONVERSATION_SESSION_KEY)
    recent_items = _load_recent_items(user=current_user)
    for item in recent_items:
        item["is_active"] = str(item["id"]) == str(active_conversation_id)

    current_messages = _load_conversation_messages(current_conversation)
    current_item = _build_conversation_item(current_conversation, active_conversation_id) if current_conversation else None
    current_debug = request.session.pop("qa_last_debug", None)

    context = {
        "error_message": error_message,
        "sources": sources,
        "items": recent_items,
        "current_conversation": current_conversation,
        "current_conversation_id": active_conversation_id,
        "current_item": current_item,
        "current_messages": current_messages,
        "current_debug": current_debug if settings.DEBUG else None,
        "current_user": current_user,
        "current_user_avatar": _safe_avatar_url(current_user),
        "medical_disclaimer": MEDICAL_DISCLAIMER,
    }
    return render(request, "AI/qa_conversation.html", context)


def qa_delete_conversation(request):
    """刪除對話（只接受 POST，並驗證對話屬於登入者）。"""
    current_user = get_current_user_profile(request)
    if not current_user:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": "登入狀態已失效，請重新登入。"}, status=401)
        return redirect('login')

    if request.method != "POST":
        return redirect(reverse("qa_conversation"))

    conversation = _get_conversation_by_id(request.POST.get("conversation_id"), current_user)
    if not conversation:
        django_messages.error(request, "找不到要刪除的對話，或它不屬於你。")
        return redirect(reverse("qa_conversation"))

    conversation_id = conversation.qaconversation_id
    try:
        with transaction.atomic():
            conversation.delete()
    except Exception:
        logger.exception("Failed to delete QA conversation %s", conversation_id)
        django_messages.error(request, "刪除對話失敗，請稍後再試。")
        return redirect(reverse("qa_conversation"))

    if str(request.session.get(ACTIVE_CONVERSATION_SESSION_KEY)) == str(conversation_id):
        _set_current_conversation_id(request, None)
        request.session["qa_skip_auto_load"] = True
        request.session.modified = True

    django_messages.success(request, "已刪除對話。")
    return redirect(reverse("qa_conversation"))
