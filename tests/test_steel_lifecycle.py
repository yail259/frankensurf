import pytest
from frankensurf import Runtime,WebPolicy
from frankensurf.runtime import WebFailure


class Response:
    status=403

class Page:
    url="https://blocked.test/"
    def __init__(self): self.closed=False; self.navigations=0
    def is_closed(self): return self.closed
    async def wait_for_timeout(self,value): pass
    async def goto(self,*args,**kwargs): self.navigations+=1; return Response()
    async def close(self): self.closed=True

class Context:
    def __init__(self,page): self.page=page; self.created=0; self.closed=False
    async def new_page(self): self.created+=1; return self.page
    async def close(self): self.closed=True

class Browser:
    def __init__(self,context): self.contexts=[context]


async def test_steel_block_keeps_owned_target_for_safe_session_release(tmp_path):
    page=Page(); context=Context(page); browser=Browser(context)
    async with Runtime(tmp_path) as web:
        async def configured(*args): return browser
        web._browser=configured
        for _ in range(2):
            with pytest.raises(WebFailure) as failure:
                await web._get_browser("https://blocked.test",WebPolicy(),"steel")
            assert failure.value.code=="BLOCKED"
        assert context.created==1
        assert not page.closed and not context.closed
        assert page.navigations==2
