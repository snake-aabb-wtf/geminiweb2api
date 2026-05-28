import json
import time
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Account:
    name: str
    f_sid: str = ""
    at: str = ""
    sn_param: str = ""
    bl_param: str = ""
    hl: str = "zh-CN"
    session_uuid: str = ""
    request_hash: str = ""
    headers: dict = field(default_factory=dict)
    enabled: bool = True
    error_count: int = 0
    last_used: float = 0.0
    bound_models: list = field(default_factory=list)


class AccountPool:
    def __init__(self, strategy: str = "least-recently-used", max_errors: int = 3):
        self.accounts: list[Account] = []
        self.strategy = strategy
        self.max_errors = max_errors
        self._stats = {"total_requests": 0, "success": 0, "failures": 0, "retries": 0}

    def add(self, account: Account):
        self.accounts.append(account)

    def select(self, model_name: str) -> Optional[Account]:
        candidates = [
            a for a in self.accounts
            if a.enabled and a.error_count < self.max_errors and model_name in a.bound_models
        ]
        if not candidates:
            return None
        if self.strategy == "least-recently-used":
            return min(candidates, key=lambda a: a.last_used)
        return candidates[0]

    def record_success(self, account: Account):
        account.error_count = 0
        account.last_used = time.time()
        self._stats["success"] += 1

    def record_failure(self, account: Account):
        account.error_count += 1
        self._stats["failures"] += 1

    def record_retry(self):
        self._stats["retries"] += 1

    def record_request(self):
        self._stats["total_requests"] += 1

    def stats(self) -> dict:
        return {
            **self._stats,
            "accounts_total": len(self.accounts),
            "accounts_enabled": sum(1 for a in self.accounts if a.enabled),
            "accounts_disabled": sum(1 for a in self.accounts if not a.enabled),
            "accounts_exhausted": sum(1 for a in self.accounts if a.error_count >= self.max_errors),
            "accounts": [
                {
                    "name": a.name,
                    "enabled": a.enabled,
                    "error_count": a.error_count,
                    "last_used": a.last_used,
                    "bound_models": a.bound_models,
                }
                for a in self.accounts
            ],
        }

    def load_from_env(self):
        prefix = "ACCOUNT_"
        accounts_data = {}
        for key, val in sorted(os.environ.items()):
            if not key.startswith(prefix):
                continue
            parts = key[len(prefix):].split("_", 1)
            if len(parts) != 2:
                continue
            acct_name, field = parts
            if acct_name not in accounts_data:
                accounts_data[acct_name] = {"name": acct_name}
            if field == "ENABLED":
                accounts_data[acct_name]["enabled"] = val.lower() in ("true", "1", "yes")
            elif field == "HEADER":
                hname, hval = val.split("=", 1) if "=" in val else (val, "")
                if "headers" not in accounts_data[acct_name]:
                    accounts_data[acct_name]["headers"] = {}
                accounts_data[acct_name]["headers"][hname] = hval
            elif field == "MODELS":
                accounts_data[acct_name]["bound_models"] = [m.strip() for m in val.split(",") if m.strip()]
            else:
                accounts_data[acct_name][field.lower()] = val

        for data in accounts_data.values():
            self.add(Account(**data))

    def get_account(self, name: str) -> Optional[Account]:
        for a in self.accounts:
            if a.name == name:
                return a
        return None

    def to_env_lines(self) -> list[str]:
        lines = []
        for a in self.accounts:
            lines.append(f"ACCOUNT_{a.name}_F_SID={a.f_sid}")
            lines.append(f"ACCOUNT_{a.name}_AT={a.at}")
            lines.append(f"ACCOUNT_{a.name}_SN_PARAM={a.sn_param}")
            lines.append(f"ACCOUNT_{a.name}_BL_PARAM={a.bl_param}")
            lines.append(f"ACCOUNT_{a.name}_HL={a.hl}")
            lines.append(f"ACCOUNT_{a.name}_UUID={a.session_uuid}")
            lines.append(f"ACCOUNT_{a.name}_HASH={a.request_hash}")
            lines.append(f"ACCOUNT_{a.name}_ENABLED={'true' if a.enabled else 'false'}")
            lines.append(f"ACCOUNT_{a.name}_MODELS={','.join(a.bound_models)}")
            lines.append("")
        return lines
