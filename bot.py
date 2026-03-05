"""Telegram → Linear bot: creates Linear issues from formatted messages."""

import logging
import os
import re
import datetime

import httpx
from telegram import Update, MessageOriginUser, MessageOriginHiddenUser
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
LINEAR_API_KEY = os.environ["LINEAR_API_KEY"]
LINEAR_API_URL = "https://api.linear.app/graphql"
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

TEAM_IDS = {
    "e": os.environ["LINEAR_TEAM_ENGINEERING"],
    "s": os.environ["LINEAR_TEAM_SALES"],
}

TEAM_LABELS = {"e": "Engineering", "s": "Sales"}
PRIORITY_LABELS = {0: None, 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}

HELP_TEXT = """Send a message in this format:

  <task name>;<flags>

Flags (after the semicolon, in any order):
  Teams:    s = Sales, e = Engineering (default: Engineering)
  Priority: u = Urgent, h = High, m = Medium, l = Low (default: none)
  Deadline: t = tomorrow, or d/m or d-m date, e.g. 15/3 (default: next Monday)

Examples:
  Fix login bug;u e t          → Engineering · Urgent · due tomorrow
  Call client back;s h 15/3   → Sales · High · due Mar 15
  Update docs                  → Engineering · no priority · due next Monday
  Deploy hotfix;u t            → Engineering · Urgent · due tomorrow

Forwarded messages:
  Forward a message here, then reply to it with task;flags.
  The forwarded text becomes the issue description."""


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def next_monday() -> datetime.date:
    today = datetime.date.today()
    days_ahead = (0 - today.weekday()) % 7  # Monday = 0
    if days_ahead == 0:
        days_ahead = 7
    return today + datetime.timedelta(days=days_ahead)


def parse_deadline(flags: str) -> tuple[datetime.date | None, str | None]:
    """Return (date, error_or_None). Modifies nothing."""
    today = datetime.date.today()

    date_match = re.search(r"(\d{1,2})[/\-](\d{1,2})", flags)
    if date_match:
        day, month = int(date_match.group(1)), int(date_match.group(2))
        try:
            d = datetime.date(today.year, month, day)
            if d < today:
                d = datetime.date(today.year + 1, month, day)
            return d, None
        except ValueError:
            return None, f"Invalid date {day}/{month} — use d/m format (e.g. 15/3)"

    # Remove any digits so they don't interfere with single-char matching
    flags_clean = re.sub(r"\d", "", flags)

    if "t" in flags_clean:
        return today + datetime.timedelta(days=1), None

    return next_monday(), None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

PRIORITY_MAP = [("u", 1), ("h", 2), ("m", 3), ("l", 4)]


def parse_message(text: str) -> dict | str:
    """Parse 'task name;flags' into a dict, or return an error string."""
    parts = text.split(";", 1)
    title = parts[0].strip()
    flags = parts[1] if len(parts) > 1 else ""

    if not title:
        return "Task name cannot be empty.\n\nFormat: <task name>;<flags>\nSend /help for details."

    # Extract date first so its digits don't interfere with char flags
    due_date, error = parse_deadline(flags)
    if error:
        return error

    # Remove date token from flags before char matching
    flags_no_date = re.sub(r"\d{1,2}[/\-]\d{1,2}", "", flags)
    # Also remove digits to clean up
    flags_no_date = re.sub(r"\d", "", flags_no_date)

    # Team
    team = "e"
    if "s" in flags_no_date:
        team = "s"

    # Priority (first match wins by priority order)
    priority = 0
    for char, val in PRIORITY_MAP:
        if char in flags_no_date:
            priority = val
            break

    return {
        "title": title,
        "team": team,
        "priority": priority,
        "due_date": due_date,
    }


# ---------------------------------------------------------------------------
# Linear client
# ---------------------------------------------------------------------------

ISSUE_CREATE_MUTATION = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      id
      identifier
      url
    }
  }
}
"""


async def _graphql(query: str, variables: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            LINEAR_API_URL,
            json={"query": query, "variables": variables or {}},
            headers={
                "Authorization": LINEAR_API_KEY,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if "errors" in data:
        raise RuntimeError(data["errors"][0]["message"])
    return data["data"]


LINEAR_VIEWER_ID: str | None = None


async def validate_linear_api() -> None:
    global LINEAR_VIEWER_ID
    data = await _graphql("{ viewer { id name } }")
    LINEAR_VIEWER_ID = data["viewer"]["id"]
    logger.info("Linear API OK — authenticated as: %s (id: %s)", data["viewer"]["name"], LINEAR_VIEWER_ID)


async def create_issue(
    title: str,
    description: str | None,
    team_id: str,
    priority: int,
    due_date: datetime.date,
) -> dict:
    variables: dict = {
        "input": {
            "title": title,
            "teamId": team_id,
            "priority": priority,
            "dueDate": due_date.isoformat(),
            "assigneeId": LINEAR_VIEWER_ID,
        }
    }
    if description:
        variables["input"]["description"] = description

    data = await _graphql(ISSUE_CREATE_MUTATION, variables)
    result = data["issueCreate"]
    if not result["success"]:
        raise RuntimeError("issueCreate returned success=false")
    return result["issue"]


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------

def _format_sender(origin) -> str:
    if isinstance(origin, MessageOriginUser):
        user = origin.sender_user
        name = user.first_name or ""
        if user.last_name:
            name = f"{name} {user.last_name}".strip()
        if user.username:
            name = f"{name} (@{user.username})".strip()
        return name or "Unknown user"
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name or "Unknown user"
    # Channel or chat origin
    chat = getattr(origin, "chat", None)
    if chat:
        return getattr(chat, "title", None) or getattr(chat, "username", "Unknown")
    return "Unknown"


def _build_fwd_description(origin, fwd_text: str) -> str:
    name = _format_sender(origin)
    date_str = origin.date.strftime("%B %d, %Y")
    return f"Forwarded from {name} on {date_str}:\n\n{fwd_text}"


WAITING_FOR_TASK = 1


async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive a forwarded message and prompt for task details."""
    message = update.message
    if not message:
        return ConversationHandler.END

    origin = getattr(message, "forward_origin", None)
    fwd_text = message.text or message.caption or ""
    description = _build_fwd_description(origin, fwd_text) if fwd_text and origin else None

    context.user_data["fwd_description"] = description

    await message.reply_text(
        "Got it! Now send me the task name and flags.\n"
        "Example: `Fix the thing;u h`\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown",
    )
    return WAITING_FOR_TASK


async def handle_task_after_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive task;flags after a forwarded message and create the issue."""
    message = update.message
    task_input = message.text or message.caption or ""
    if not task_input:
        return WAITING_FOR_TASK

    description = context.user_data.pop("fwd_description", None)
    await _create_and_reply(message, task_input, description)
    return ConversationHandler.END


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("fwd_description", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle regular (non-forwarded) messages."""
    message = update.message
    if not message:
        return

    raw = message.text or message.caption or ""
    if not raw:
        return

    await _create_and_reply(message, raw, description=None)


async def _create_and_reply(message, task_input: str, description: str | None) -> None:
    """Parse task input, create a Linear issue, and reply."""
    parsed = parse_message(task_input)
    if isinstance(parsed, str):
        await message.reply_text(parsed)
        return

    team_id = TEAM_IDS[parsed["team"]]

    try:
        issue = await create_issue(
            title=parsed["title"],
            description=description,
            team_id=team_id,
            priority=parsed["priority"],
            due_date=parsed["due_date"],
        )
    except Exception as exc:
        logger.exception("Failed to create Linear issue")
        await message.reply_text(f"Failed to create issue: {exc}")
        return

    team_label = TEAM_LABELS[parsed["team"]]
    priority_label = PRIORITY_LABELS[parsed["priority"]]
    date_str = parsed["due_date"].strftime("%b ") + str(parsed["due_date"].day)

    parts = [team_label]
    if priority_label:
        parts.append(priority_label)
    parts.append(f"Due {date_str}")

    await message.reply_text(
        f"\u2713 {parsed['title']}\n{' \u00b7 '.join(parts)}\n{issue['url']}"
    )


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hello! Send me a task and I'll create a Linear issue.\n\n"
        "Format: <task name>;<flags>\n\n"
        "Send /help for the full reference."
    )


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    await validate_linear_api()


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    allowed = filters.User(user_id=ALLOWED_USER_ID)

    app.add_handler(CommandHandler("start", handle_start, filters=allowed))
    app.add_handler(CommandHandler("help", handle_help, filters=allowed))

    # Forwarded messages: two-step conversation (forward → task;flags)
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                allowed & filters.FORWARDED & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
                handle_forward,
            ),
        ],
        states={
            WAITING_FOR_TASK: [
                CommandHandler("cancel", handle_cancel),
                MessageHandler(
                    allowed & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
                    handle_task_after_forward,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", handle_cancel)],
    )
    app.add_handler(conv_handler)

    # Regular (non-forwarded) messages
    app.add_handler(
        MessageHandler(
            allowed & ~filters.FORWARDED & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
