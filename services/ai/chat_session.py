from typing import Dict, List


class ChatSession:
    def __init__(self, *, system_prompt: str, max_rounds: int = 8):
        self._system_prompt = str(system_prompt or "").strip() or "You are a helpful assistant."
        self._max_rounds = max(2, int(max_rounds or 8))
        self._messages: List[Dict[str, str]] = [{"role": "system", "content": self._system_prompt}]

    def has_history(self) -> bool:
        return len(self._messages) > 1

    def preview_with_user(self, user_content: str) -> List[Dict[str, str]]:
        content = str(user_content or "").strip()
        if not content:
            return [dict(m) for m in self._messages]
        return [dict(m) for m in self._messages] + [{"role": "user", "content": content}]

    def commit_turn(self, *, user_content: str, assistant_content: str) -> None:
        user_text = str(user_content or "").strip()
        if not user_text:
            return
        self._messages.append({"role": "user", "content": user_text})
        assistant_text = str(assistant_content or "").strip()
        if assistant_text:
            self._messages.append({"role": "assistant", "content": assistant_text})
        self._trim()

    def _trim(self) -> None:
        if len(self._messages) <= 1:
            return
        max_tail = self._max_rounds * 2
        tail = self._messages[1:]
        if len(tail) <= max_tail:
            return
        self._messages = [self._messages[0]] + tail[-max_tail:]

    def reset(self) -> None:
        self._messages = [{"role": "system", "content": self._system_prompt}]
