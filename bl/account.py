"""Signing in and out."""

import bpy
import bpy.utils.previews

from ..core import auth
from . import jobs
from .library import load_owned
from .search import start_search
from .store import HOME, Result, session, state, wm_of


class QUIXLEND_OT_login_open(bpy.types.Operator):
    bl_idname = "quixlend.login_open"
    bl_label = "Open Epic Login"
    bl_description = "Open the Epic Games sign-in page in your browser"

    def execute(self, context: bpy.types.Context) -> Result:
        bpy.ops.wm.url_open(url=auth.LOGIN_URL)
        return {"FINISHED"}


class QUIXLEND_OT_login(bpy.types.Operator):
    bl_idname = "quixlend.login"
    bl_label = "Log In"
    bl_description = "Finish signing in with the pasted authorization code"

    def execute(self, context: bpy.types.Context) -> Result:
        wm = wm_of(context)
        code = wm.quixlend_code
        state.status = "Logging in…"

        def done(_, error):
            if error:
                state.status = f"Login failed: {error}"[:160]
                return
            wm.quixlend_code = ""
            load_owned()
            start_search()

        jobs.run(lambda: session.login_with_code(code), done)
        return {"FINISHED"}


class QUIXLEND_OT_logout(bpy.types.Operator):
    bl_idname = "quixlend.logout"
    bl_label = "Log Out"
    bl_description = "Forget the stored Quixel session"

    def execute(self, context: bpy.types.Context) -> Result:
        session.logout()
        (HOME / "owned.json").unlink(missing_ok=True)
        state.owned, state.status = None, ""
        return {"FINISHED"}
