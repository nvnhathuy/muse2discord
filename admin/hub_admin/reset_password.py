"""Server-side admin password reset CLI (fallback path).

Run ON the server by the admin personally - no agent involved:

    python3 -m hub_admin.reset_password [--state-dir DIR]

Asks for the new password twice (hidden input), stores only its hash,
and bumps the credentials version so every existing admin session dies
on its next check - nobody stays logged in with the old password.
Step-by-step for non-technical users: HUONG-DAN-RESET-MAT-KHAU.md.

For scripts/tests the new password can come from the environment
variable HUB_ADMIN_NEW_PASSWORD instead of the interactive prompt.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .credentials import MIN_PASSWORD_LENGTH, AdminCredentials


def reset_password(state_dir: str, new_password: str) -> str:
    """Reset the admin password stored under state_dir. Returns the
    credentials file path. Raises ValueError on a too-short password."""
    credentials = AdminCredentials(os.path.join(state_dir, "admin_credentials.json"))
    credentials.set_password(new_password)
    return credentials.path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reset the hub admin page password")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("HUB_ADMIN_STATE_DIR", "./state"),
        help="hub admin state directory (default: $HUB_ADMIN_STATE_DIR or ./state)",
    )
    args = parser.parse_args(argv)

    new_password = os.environ.get("HUB_ADMIN_NEW_PASSWORD", "")
    if not new_password:
        new_password = getpass.getpass("Mat khau quan tri moi: ")
        confirm = getpass.getpass("Nhap lai mat khau moi: ")
        if new_password != confirm:
            print("Hai lan nhap khong giong nhau. Chua doi gi ca.", file=sys.stderr)
            return 1
    try:
        path = reset_password(args.state_dir, new_password)
    except ValueError as exc:
        print(f"Khong dat lai duoc: {exc}", file=sys.stderr)
        return 1
    print(f"Da dat lai mat khau quan tri (luu bam tai {path}).")
    print("Tat ca phien dang nhap cu da bi huy. Hay dang nhap lai bang mat khau moi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
