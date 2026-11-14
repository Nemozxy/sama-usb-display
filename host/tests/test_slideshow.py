from types import SimpleNamespace

from PIL import Image

from dashboard_app import DashboardApp


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def slideshow_app(paths):
    obj = DashboardApp.__new__(DashboardApp)
    obj.picture_paths = paths
    obj.picture_index = 0
    obj.picture_path = paths[0]
    obj.picture_image = None
    obj.image = None
    obj.mode = Value('图片')
    obj.content_text = Value('')
    obj.status = Value('')
    obj.update_preview = lambda: None
    obj.live = SimpleNamespace(submitted=[], submit=lambda image: obj.live.submitted.append(image))
    obj._live_accepts_frames = lambda: True
    return obj


def test_picture_slideshow_advances_and_wraps(tmp_path):
    paths = []
    for index, color in enumerate(('red', 'green', 'blue')):
        path = tmp_path / f'{index}.png'
        Image.new('RGB', (32, 32), color).save(path)
        paths.append(str(path))
    obj = slideshow_app(paths)
    assert DashboardApp._advance_picture(obj)
    assert obj.picture_index == 1 and obj.image.getpixel((360, 780)) == (0, 128, 0)
    assert DashboardApp._advance_picture(obj)
    assert obj.picture_index == 2
    assert DashboardApp._advance_picture(obj)
    assert obj.picture_index == 0 and obj.image.getpixel((360, 780)) == (255, 0, 0)
    assert len(obj.live.submitted) == 3


def test_picture_slideshow_skips_unreadable_files(tmp_path):
    valid = tmp_path / 'valid.png'
    Image.new('RGB', (32, 32), 'blue').save(valid)
    bad = tmp_path / 'bad.png'
    bad.write_bytes(b'not an image')
    obj = slideshow_app([str(valid), str(bad), str(valid)])
    assert DashboardApp._advance_picture(obj)
    assert obj.picture_index == 2 and obj.picture_path == str(valid)
