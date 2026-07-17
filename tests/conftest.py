from decimal import Decimal

import pytest

from library.models import Album, Artist, LibraryRoot, Track
from library.services import normalize_text


@pytest.fixture
def artist(db):
    return Artist.objects.create(name="Deftones", sort_name="Deftones", normalized_name="deftones")


@pytest.fixture
def album(artist):
    return Album.objects.create(
        artist=artist, title="White Pony", normalized_title="white pony", year=2000
    )


@pytest.fixture
def root(db, tmp_path):
    return LibraryRoot.objects.create(path=str(tmp_path))


@pytest.fixture
def track(root, artist, album):
    return Track.objects.create(
        library_root=root,
        artist=artist,
        album=album,
        full_path=f"{root.path}/Change.mp3",
        relative_path="Change.mp3",
        file_format="mp3",
        title="Change (In the House of Flies)",
        normalized_title=normalize_text("Change (In the House of Flies)"),
        year=2000,
        duration_seconds=Decimal("240"),
        file_size=100,
        file_modified_ns=1,
    )
