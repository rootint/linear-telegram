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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    origin = getattr(message, "forward_origin", None)
    reply = message.reply_to_message
    reply_origin = getattr(reply, "forward_origin", None) if reply else None

    if reply_origin:
        # User replied to a forwarded message with task;flags — the intended flow.
        # The reply text is the task input; the original forwarded message is the description.
        task_input = message.text or message.caption or ""
        if not task_input:
            return
        fwd_text = reply.text or reply.caption or ""
        description = _build_fwd_description(reply_origin, fwd_text) if fwd_text else None

    elif origin:
        # Bare forwarded message (no reply-based task input).
        # For media with a user caption, treat caption as task;flags.
        # For plain text forwards, silently ignore — user should reply with task;flags.
        if message.caption:
            task_input = message.caption
            description = _build_fwd_description(origin, message.text or "")
        else:
            # Plain text forward with no caption — prompt user to reply with task;flags
            await message.reply_text(
                "Reply to this message with your task name and flags to create a Linear issue.\n"
                "Example: `Fix the thing;u h`",
                parse_mode="Markdown",
            )
            return

    else:
        # Regular message
        raw = message.text or message.caption or ""
        if not raw:
            return
        task_input = raw
        description = None

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
    app.add_handler(
        MessageHandler(
            allowed & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
