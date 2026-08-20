"""連線設定的讀寫。

明文 token 只在三個地方短暫存在：使用者送出的表單、驗證時的 HTTP 標頭、
以及寫入前的加密呼叫。其餘任何地方（資料庫、log、頁面）都只有密文或遮罩。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from devloop.core.crypto import decrypt, encrypt, mask
from devloop.db.models import Connection
from devloop.jira.client import HttpJiraClient, JiraClient, JiraError


@dataclass(frozen=True)
class WorkspaceStatus:
    """被管理的專案目錄長什麼樣 —— claude 會在這裡面跑，所以要先看清楚。"""

    path: str
    exists: bool
    is_dir: bool
    is_git_repo: bool

    @property
    def error(self) -> str | None:
        if not self.path:
            return "還沒設定專案根目錄"
        if not self.exists:
            return f"找不到這個路徑：{self.path}"
        if not self.is_dir:
            return f"這不是資料夾：{self.path}"
        return None

    @property
    def warning(self) -> str | None:
        if self.error is None and not self.is_git_repo:
            return "這個目錄不是 git repo —— claude 改壞了沒有還原點"
        return None


def inspect_workspace(path: str) -> WorkspaceStatus:
    candidate = Path(path).expanduser() if path else Path()
    exists = bool(path) and candidate.exists()
    is_dir = exists and candidate.is_dir()
    return WorkspaceStatus(
        path=path,
        exists=exists,
        is_dir=is_dir,
        is_git_repo=is_dir and (candidate / ".git").exists(),
    )


@dataclass(frozen=True)
class ConnectionView:
    """給頁面看的形狀 —— 刻意沒有明文 token 這個欄位。"""

    jira_site: str
    jira_email: str
    jira_project: str
    workspace: WorkspaceStatus
    token_masked: str
    display_name: str | None
    verified_at: datetime | None
    last_error: str | None

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None and not self.last_error

    @property
    def is_usable(self) -> bool:
        """憑證通了、工作目錄也對，才真的能跑 job。"""
        return self.is_verified and self.workspace.error is None


def get_connection(session: Session, owner_key: str) -> Connection | None:
    return session.scalar(select(Connection).where(Connection.owner_key == owner_key))


def to_view(conn: Connection) -> ConnectionView:
    return ConnectionView(
        jira_site=conn.jira_site,
        jira_email=conn.jira_email,
        jira_project=conn.jira_project,
        workspace=inspect_workspace(conn.workspace_root),
        token_masked=mask(decrypt(conn.jira_token_encrypted)),
        display_name=conn.display_name,
        verified_at=conn.verified_at,
        last_error=conn.last_error,
    )


def client_for(conn: Connection) -> JiraClient:
    return HttpJiraClient(conn.jira_site, conn.jira_email, decrypt(conn.jira_token_encrypted))


def save_connection(
    session: Session,
    owner_key: str,
    *,
    site: str,
    email: str,
    project: str,
    token: str | None,
    workspace: str = "",
    verifier: JiraClient | None = None,
) -> ConnectionView:
    """存檔並立刻驗證。token 傳 None 代表「沿用既有的，我沒有要換」。

    驗證失敗不會拒絕存檔 —— 設定仍然留著，錯誤訊息一起留著讓使用者看得到
    哪裡不對。但 verified_at 會被清掉，沒驗過的連線不會被拿去跑。
    """
    conn = get_connection(session, owner_key)
    if conn is None:
        if not token:
            raise ValueError("第一次設定必須填入 API token")
        conn = Connection(
            owner_key=owner_key,
            jira_site=site,
            jira_email=email,
            jira_project=project,
            workspace_root=workspace,
            jira_token_encrypted=encrypt(token),
        )
        session.add(conn)
    else:
        conn.jira_site = site
        conn.jira_email = email
        conn.jira_project = project
        conn.workspace_root = workspace
        if token:
            conn.jira_token_encrypted = encrypt(token)

    session.flush()

    probe = verifier if verifier is not None else client_for(conn)
    try:
        identity = probe.verify()
    except JiraError as exc:
        conn.verified_at = None
        conn.last_error = str(exc)
    else:
        conn.jira_account_id = identity.account_id
        conn.display_name = identity.display_name
        conn.verified_at = datetime.now(UTC)
        conn.last_error = None

    session.flush()
    return to_view(conn)
