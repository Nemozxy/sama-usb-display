import gc
import tkinter as tk

import dashboard_app
from dashboard_settings import DEFAULTS


def test_three_function_tabs_and_default_layout_fit(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard_app, 'load_settings', lambda: dict(DEFAULTS))
    monkeypatch.setattr(dashboard_app, 'save_settings', lambda *args: None)
    root = tk.Tk()
    root.withdraw()
    try:
        app = dashboard_app.DashboardApp(root, mock=True)
        from PIL import Image
        picture = tmp_path / 'picture.png'
        media = tmp_path / 'loop.gif'
        Image.new('RGB', (16, 16), 'blue').save(picture)
        Image.new('RGB', (16, 16), 'blue').save(media)
        app.picture_path = str(picture)
        app.picture_paths = [str(picture)]
        app.picture_image = dashboard_app.fit_picture(picture)
        app.media_path = str(media)
        root.update()
        assert [app.notebook.tab(tab, 'text').strip() for tab in app.notebook.tabs()] == [
            '监控面板', '板载媒体', 'Windows 桌面']
        assert root.winfo_reqheight() <= root.winfo_height()
        assert root.winfo_reqwidth() <= root.winfo_width()
        assert app.start_button.winfo_ismapped() or not root.winfo_viewable()
        assert app.stop_button.winfo_ismapped() or not root.winfo_viewable()
        app.notebook.select(app.media_tab)
        root.update()
        assert app.mode.get() == '板载媒体'
        app.onboard_kind.set('image')
        app._onboard_kind_changed()
        assert app.onboard_path.get() == str(picture)
        app.notebook.select(app.desktop_tab)
        root.update()
        assert app.mode.get() == 'Windows 桌面'
        app.close()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
        gc.collect()
