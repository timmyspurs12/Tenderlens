# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
# GenLayer Direct Mode runner smoke fixture; this is not a procurement evaluation contract.

import genlayer as gl


class Storage(gl.Contract):
    value: str

    def __init__(self, initial_value: str):
        self.value = initial_value

    @gl.public.view
    def get_value(self) -> str:
        return self.value

    @gl.public.write
    def set_value(self, value: str) -> None:
        self.value = value
