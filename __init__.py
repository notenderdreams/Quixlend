from .bl import jobs, sidebar, ui


def register():
    jobs.register()
    ui.register()
    sidebar.register()


def unregister():
    sidebar.unregister()
    ui.unregister()
    jobs.unregister()
