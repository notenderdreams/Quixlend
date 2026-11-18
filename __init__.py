from .bl import jobs, ui
from .bl.sidebar import install as sidebar


def register():
    jobs.register()
    ui.register()
    sidebar.register()


def unregister():
    sidebar.unregister()
    ui.unregister()
    jobs.unregister()
