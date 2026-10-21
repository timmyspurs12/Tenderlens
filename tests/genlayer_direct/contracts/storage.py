# { "Depends": "py-genlayer:1zr6nqk597d97kg0dyxg0shhrykx5v02zjgnyrajapy4wlqvfvwh" }
# GenLayer Direct Mode runner smoke fixture; this is not a procurement evaluation contract.

import genlayer as gl


class Storage(gl.contract.Contract):
    value: str

    def __init__(self, initial_value: str):
        self.value = initial_value

    @gl.public.view
    def get_value(self) -> str:
        return self.value

    @gl.public.write
    def set_value(self, value: str) -> None:
        self.value = value
