from typing import Any, Dict


class DispatchAPI:
    def request(self, task_name: str, payload: Dict[str, Any]) -> Any:
        raise NotImplementedError()

