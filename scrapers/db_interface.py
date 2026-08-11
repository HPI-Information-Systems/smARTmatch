"""Simplified SQLAlchemy-based database interface for smARTmatch scrapers.

Provides IDE autocomplete and type checking through ORM models.

Example usage:
    from scrapers.db_interface import Database
    from scrapers.models_production import Artist, AuctionArtwork, AuctionPlatform

    db = Database()

    # Create an artist
    artist = db.get_or_create_artist(
        first_name="Vincent",
        last_name="van Gogh"
    )

    # Create an auction platform
    platform = db.get_or_create_auction_platform(
        name="Christie's",
        email="info@christies.com"
    )

    # Upsert an artwork row and attach downloaded image files
    artwork = db.upsert_auction_artwork(
        lot_id="12345",
        title="Sunflowers",
        artist_id=artist.artist_id,
        auction_platform_id=platform.auction_platform_id,
    )
    db.set_auction_artwork_images(
        auction_artwork_id=artwork.auction_artwork_id,
        image_paths=["db/data-production/images/example.jpg"],
    )
    db.commit()
"""

import os
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Sequence, Type, TypeVar
from uuid import UUID

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, MultipleResultsFound
from sqlalchemy.orm import Session, load_only, sessionmaker


def _load_dotenv_files() -> None:
    """Best-effort loading of .env files for local development.

    Docker Compose commonly uses `db/.env`, but Python processes don't
    automatically read it
    """

    def _load_dotenv_fallback(path: Path) -> None:
        if not path.exists() or not path.is_file():
            return

        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)

    try:
        from dotenv import load_dotenv  # type: ignore

        has_python_dotenv = True
    except Exception:
        has_python_dotenv = False

    repo_root = Path(__file__).resolve().parents[1]
    # Load in increasing precedence
    dotenv_paths = [repo_root / ".env", repo_root / "db" / ".env"]

    if has_python_dotenv:
        for dotenv_path in dotenv_paths:
            load_dotenv(dotenv_path)
        return

    for dotenv_path in dotenv_paths:
        _load_dotenv_fallback(dotenv_path)


_load_dotenv_files()

from .models_production import (  # noqa: E402
    Artist,
    ArtistNameVariants,
    AuctionArtwork,
    AuctionArtworkImageFile,
    Auctioneer,
    AuctionPlatform,
    Base,
    Contact,
    Country,
    Expert,
    ImageFile,
    Institution,
    LiteratureSource,
    Location,
    LocationNameVariants,
    LostArtwork,
    LostArtworkImageFile,
)

__all__ = [
    "Database",
    "DatabaseError",
    "Base",
    "Artist",
    "ArtistNameVariants",
    "AuctionArtwork",
    "AuctionArtworkImageFile",
    "AuctionPlatform",
    "Auctioneer",
    "Contact",
    "Country",
    "Expert",
    "ImageFile",
    "Institution",
    "LiteratureSource",
    "Location",
    "LocationNameVariants",
    "LostArtwork",
    "LostArtworkImageFile",
]

T = TypeVar("T", bound=Base)


POSTGRES_ENV_VARS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    database: str
    user: str
    password: str


class DatabaseError(Exception):
    """Raised when a database operation fails."""

    pass


class Database:
    """Simple SQLAlchemy database interface with IDE support.

    Usage:
        # Context manager (auto-commits on success, rolls back on error)
        with Database() as db:
            artist = db.get_or_create_artist(last_name="Picasso")
            db.add(AuctionArtwork(title="Test", artist_id=artist.artist_id))

        # Manual session management
        db = Database()
        artist = db.get_or_create_artist(last_name="Monet")
        db.commit()
        db.close()
    """

    def __init__(self):
        """Initialize a database connection from required POSTGRES_* env vars."""

        settings = self._postgres_settings_from_env()
        try:
            self.engine = create_engine(
                self._engine_target(settings),
                echo=False,
                connect_args={"connect_timeout": 5},
            )
            self.SessionLocal = sessionmaker(bind=self.engine)
            self.session: Optional[Session] = None
            self._validated_connection: bool = False
            self._table_columns_cache: dict[str, set[str]] = {}
        except Exception as e:
            raise DatabaseError(f"Failed to connect to database: {e}") from e

    def _validate_connection(self) -> None:
        """Fail fast if the database is unreachable.

        This is intentionally lightweight (a single `SELECT 1`) and is only run
        once per Database instance.
        """

        if self._validated_connection:
            return

        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            self._validated_connection = True
        except Exception as e:
            raise DatabaseError(
                "Database connection failed. Ensure Postgres is running and "
                "POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, "
                "and POSTGRES_PASSWORD are set."
                f" Original error: {e}"
            ) from e

    @classmethod
    def _postgres_settings_from_env(cls) -> PostgresSettings:
        missing = [name for name in POSTGRES_ENV_VARS if not cls._env_value(name)]
        if missing:
            raise DatabaseError(
                "Missing required PostgreSQL environment variable(s): "
                + ", ".join(missing)
            )

        port_value = cls._env_value("POSTGRES_PORT")
        try:
            port = int(port_value or "")
        except ValueError as exc:
            raise DatabaseError(
                f"POSTGRES_PORT must be an integer, got {port_value!r}"
            ) from exc
        if port <= 0:
            raise DatabaseError(f"POSTGRES_PORT must be positive, got {port}")

        return PostgresSettings(
            host=cls._env_value("POSTGRES_HOST") or "",
            port=port,
            database=cls._env_value("POSTGRES_DB") or "",
            user=cls._env_value("POSTGRES_USER") or "",
            password=cls._env_value("POSTGRES_PASSWORD") or "",
        )

    @staticmethod
    def _env_value(name: str) -> str | None:
        value = os.getenv(name)
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _engine_target(settings: PostgresSettings) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=settings.user,
            password=settings.password,
            host=settings.host,
            port=settings.port,
            database=settings.database,
        )

    def __enter__(self) -> "Database":
        """Start a new database session."""
        self._validate_connection()
        self.session = self.SessionLocal()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Commit or rollback and close session."""
        if self.session:
            if exc_type is None:
                try:
                    self.session.commit()
                except Exception:
                    self.session.rollback()
                    raise
            else:
                self.session.rollback()
            self.session.close()
            self.session = None

    def _get_session(self) -> Session:
        """Get the current session or create a new one."""
        self._validate_connection()
        if self.session is None:
            self.session = self.SessionLocal()
        return self.session

    def add(self, instance: Base) -> None:
        """Add an object to the session."""
        session = self._get_session()
        session.add(instance)

    def add_all(self, instances) -> None:
        """Add multiple objects to the session."""
        session = self._get_session()
        session.add_all(list(instances))

    def flush(self) -> None:
        """Flush pending changes (assigns PKs without committing)."""
        session = self._get_session()
        session.flush()

    def commit(self) -> None:
        """Commit the current transaction."""
        session = self._get_session()
        try:
            session.commit()
        except Exception as e:
            session.rollback()
            raise DatabaseError(f"Commit failed: {e}") from e

    def rollback(self) -> None:
        """Rollback the current transaction."""
        session = self._get_session()
        session.rollback()

    def close(self) -> None:
        """Close the current session."""
        if self.session:
            self.session.close()
            self.session = None

    def query(self, model: Type[T]) -> "Session":
        """Get a query object for the given model."""
        session = self._get_session()
        return session.query(model)

    def get_by_id(self, model: Type[T], id_value: UUID) -> Optional[T]:
        """Get an object by its primary key."""
        session = self._get_session()
        return session.get(model, id_value)

    @staticmethod
    def _normalize_lookup_value(value: str) -> str:
        return " ".join(value.split()).strip()

    def _execute_first_or_none(self, stmt):
        session = self._get_session()
        result = session.execute(stmt)

        scalars = getattr(result, "scalars", None)
        if callable(scalars):
            scalar_result = scalars()
            first = getattr(scalar_result, "first", None)
            if callable(first):
                return first()

        scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
        if callable(scalar_one_or_none):
            try:
                return scalar_one_or_none()
            except MultipleResultsFound:
                retry = session.execute(stmt)
                retry_scalars = getattr(retry, "scalars", None)
                if callable(retry_scalars):
                    retry_scalar_result = retry_scalars()
                    retry_first = getattr(retry_scalar_result, "first", None)
                    if callable(retry_first):
                        return retry_first()
                return None

        first = getattr(result, "first", None)
        if callable(first):
            row = first()
            if isinstance(row, tuple):
                return row[0] if row else None
            return row

        return None

    def _model_matches_live_table(self, *, table_name: str, model) -> bool:
        """Return True when mapped model columns are present in live DB table.

        When this check cannot run (e.g. in unit tests with stub sessions), we
        return True to preserve existing ORM-based behavior.
        """
        try:
            actual_columns = self._get_table_columns(table_name)
            if not actual_columns:
                return False

            model_columns = {column.name for column in model.__table__.columns}
            return model_columns.issubset(actual_columns)
        except Exception:
            return True

    @staticmethod
    def _clean_variant_names(variant_name: Optional[list[str]]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for value in variant_name or []:
            if not isinstance(value, str):
                continue
            cleaned = value.strip()
            if not cleaned:
                continue
            lowered = cleaned.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            unique.append(cleaned)
        return unique

    @staticmethod
    def _savepoint_context(session: Session):
        begin_nested = getattr(session, "begin_nested", None)
        if callable(begin_nested):
            return begin_nested(), True
        return nullcontext(), False

    @staticmethod
    def _safe_session_rollback(session: Session) -> None:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            rollback()

    # for common operations:

    def get_or_create_artist(
        self,
        complete_name: Optional[str] = None,
        variant_name: Optional[list[str]] = None,
        **kwargs,
    ) -> Artist:
        """Get or create an artist by complete_name.

        Backward compatibility:
        - accepts legacy kwargs `first_name` and `last_name`
        - if `complete_name` is omitted, derives it from first/last

        Compatibility note:
        - falls back to SQL-column-aware upsert when live schema is older than
          the generated ORM model (common in local DB snapshots).
        """
        session = self._get_session()

        first_name = kwargs.pop("first_name", None)
        last_name = kwargs.pop("last_name", None)

        normalized_complete_name = self._normalize_lookup_value(complete_name or "")
        if not normalized_complete_name:
            parts = [
                self._normalize_lookup_value(p)
                for p in [first_name, last_name]
                if isinstance(p, str) and self._normalize_lookup_value(p)
            ]
            normalized_complete_name = self._normalize_lookup_value(" ".join(parts))

        if not normalized_complete_name:
            raise ValueError(
                "get_or_create_artist requires complete_name or first_name/last_name"
            )

        cleaned_variants = self._clean_variant_names(variant_name)

        if self._model_matches_live_table(table_name="artist", model=Artist):
            artist = self._execute_first_or_none(
                select(Artist).where(
                    func.lower(Artist.complete_name)
                    == normalized_complete_name.lower()
                )
            )

            created_artist = False
            if artist is None:
                savepoint_ctx, has_savepoint = self._savepoint_context(session)
                try:
                    with savepoint_ctx:
                        artist = Artist(
                            complete_name=normalized_complete_name,
                            **kwargs,
                        )
                        session.add(artist)
                        session.flush()
                    created_artist = True
                except IntegrityError:
                    if not has_savepoint:
                        self._safe_session_rollback(session)
                    artist = self._execute_first_or_none(
                        select(Artist).where(
                            func.lower(Artist.complete_name)
                            == normalized_complete_name.lower()
                        )
                    )
                    if artist is None:
                        raise

            if created_artist:
                existing_variants = set()
                for variant_clean in cleaned_variants:
                    lowered = variant_clean.lower()
                    if lowered in existing_variants:
                        continue
                    session.add(
                        ArtistNameVariants(
                            artist_id=artist.artist_id,
                            name_variant=variant_clean,
                        )
                    )
                    existing_variants.add(lowered)

            return artist

        # Fallback for DBs where ORM model is ahead of applied schema.
        existing_row = session.execute(
            text(
                """
                select artist_id
                from artist
                where lower(complete_name) = :complete_name
                order by artist_id
                limit 1
                """
            ),
            {"complete_name": normalized_complete_name.lower()},
        ).first()
        if existing_row:
            return SimpleNamespace(
                artist_id=existing_row[0],
                complete_name=normalized_complete_name,
            )

        actual_columns = self._get_table_columns("artist")
        values: dict[str, object] = {
            "complete_name": normalized_complete_name,
        }
        for key, value in kwargs.items():
            if key in actual_columns and value is not None:
                values[key] = value

        if "variant_name" in actual_columns and cleaned_variants:
            values["variant_name"] = cleaned_variants

        insert_columns = ", ".join(values.keys())
        insert_params = ", ".join(f":{column}" for column in values)
        created_artist = False
        savepoint_ctx, has_savepoint = self._savepoint_context(session)
        try:
            with savepoint_ctx:
                artist_id = session.execute(
                    text(
                        f"""
                        insert into artist ({insert_columns})
                        values ({insert_params})
                        returning artist_id
                        """
                    ),
                    values,
                ).scalar_one()
            created_artist = True
        except IntegrityError:
            if not has_savepoint:
                self._safe_session_rollback(session)
            existing_row = session.execute(
                text(
                    """
                    select artist_id
                    from artist
                    where lower(complete_name) = :complete_name
                    order by artist_id
                    limit 1
                    """
                ),
                {"complete_name": normalized_complete_name.lower()},
            ).first()
            if not existing_row:
                raise
            artist_id = existing_row[0]

        if created_artist:
            variant_columns = self._get_table_columns("artist_name_variants")
            if {
                "artist_id",
                "name_variant",
            }.issubset(variant_columns):
                for variant_clean in cleaned_variants:
                    session.execute(
                        text(
                            """
                            insert into artist_name_variants (artist_id, name_variant)
                            values (:artist_id, :name_variant)
                            """
                        ),
                        {
                            "artist_id": artist_id,
                            "name_variant": variant_clean,
                        },
                    )

        return SimpleNamespace(artist_id=artist_id, complete_name=normalized_complete_name)

    def get_or_create_auction_platform(self, name: str, **kwargs) -> AuctionPlatform:
        """Get or create an auction platform by name."""
        session = self._get_session()
        normalized_name = self._normalize_lookup_value(name)
        if not normalized_name:
            raise ValueError("get_or_create_auction_platform requires a non-empty name")

        platform = self._execute_first_or_none(
            select(AuctionPlatform).where(
                func.lower(AuctionPlatform.name) == normalized_name.lower()
            )
        )

        if platform is None:
            platform = AuctionPlatform(name=normalized_name, **kwargs)
            session.add(platform)
            session.flush()

        return platform

    def get_or_create_auctioneer(self, name: str, **kwargs) -> Auctioneer:
        """Get or create an auctioneer by name."""
        session = self._get_session()
        normalized_name = self._normalize_lookup_value(name)
        if not normalized_name:
            raise ValueError("get_or_create_auctioneer requires a non-empty name")

        auctioneer = self._execute_first_or_none(
            select(Auctioneer).where(
                func.lower(Auctioneer.name) == normalized_name.lower()
            )
        )

        if auctioneer is None:
            auctioneer = Auctioneer(name=normalized_name, **kwargs)
            session.add(auctioneer)
            session.flush()

        return auctioneer

    def get_or_create_expert(
        self, *, first_name: str, last_name: str, **kwargs
    ) -> Expert:
        """Get or create an expert by name."""
        session = self._get_session()

        expert = session.execute(
            select(Expert).where(
                Expert.first_name == first_name,
                Expert.last_name == last_name,
            )
        ).scalar_one_or_none()

        if expert is None:
            expert = Expert(first_name=first_name, last_name=last_name, **kwargs)
            session.add(expert)
            session.flush()

        return expert

    def get_or_create_institution(self, name: str, **kwargs) -> Institution:
        """Get or create an institution by name."""
        session = self._get_session()

        institution = session.execute(
            select(Institution).where(Institution.name == name)
        ).scalar_one_or_none()

        if institution is None:
            institution = Institution(name=name, **kwargs)
            session.add(institution)
            session.flush()

        return institution

    def get_or_create_location(
        self, location_name: str, country: Optional[str] = None, **kwargs
    ) -> Location:
        """Get or create a location by name."""
        session = self._get_session()

        stmt = select(Location).where(Location.location_name == location_name)
        if country:
            stmt = stmt.where(Location.country == country)

        location = session.execute(stmt).scalar_one_or_none()

        if location is None:
            location = Location(location_name=location_name, country=country, **kwargs)
            session.add(location)
            session.flush()

        return location

    def get_or_create_literature_source(self, title: str, **kwargs) -> LiteratureSource:
        """Get or create a literature source by title."""
        session = self._get_session()

        source = session.execute(
            select(LiteratureSource).where(LiteratureSource.title == title)
        ).scalar_one_or_none()

        if source is None:
            source = LiteratureSource(title=title, **kwargs)
            session.add(source)
            session.flush()

        return source

    def create_contact(
        self,
        *,
        first_name: str,
        last_name: str,
        institution_id: Optional[UUID] = None,
        role: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Contact:
        """Create a contact row (no de-duplication)."""
        session = self._get_session()
        contact = Contact(
            first_name=first_name,
            last_name=last_name,
            role=role,
            phone=phone,
            email=email,
            institution_id=institution_id,
        )
        session.add(contact)
        session.flush()
        return contact

    def _get_table_columns(self, table_name: str) -> set[str]:
        cache = getattr(self, "_table_columns_cache", None)
        if cache is None:
            cache = {}
            self._table_columns_cache = cache

        if table_name in cache:
            return cache[table_name]

        session = self._get_session()
        rows = session.execute(
            text(
                """
                select column_name
                from information_schema.columns
                where table_schema = current_schema() and table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).all()
        columns = {column_name for (column_name,) in rows}
        cache[table_name] = columns
        return columns

    def _table_has_columns(self, table_name: str, required_columns: set[str]) -> bool:
        return required_columns.issubset(self._get_table_columns(table_name))

    @staticmethod
    def _normalize_image_paths(image_paths: Sequence[str] | None) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        if not image_paths:
            return normalized

        for value in image_paths:
            if value is None:
                continue
            candidate = str(value).strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            normalized.append(candidate)

        return normalized

    def _image_file_id_by_path(self, *, file_path: str) -> object | None:
        session = self._get_session()
        return session.execute(
            text(
                """
                select image_file_id
                from image_file
                where file_path = :file_path
                order by image_file_id
                limit 1
                """
            ),
            {"file_path": file_path},
        ).scalar_one_or_none()

    def _ensure_image_file_id(self, *, file_path: str) -> object | None:
        existing_id = self._image_file_id_by_path(file_path=file_path)
        if existing_id is not None:
            return existing_id

        session = self._get_session()
        savepoint_ctx, has_savepoint = self._savepoint_context(session)
        try:
            with savepoint_ctx:
                return session.execute(
                    text(
                        """
                        insert into image_file (file_path)
                        values (:file_path)
                        returning image_file_id
                        """
                    ),
                    {"file_path": file_path},
                ).scalar_one()
        except IntegrityError:
            if not has_savepoint:
                self._safe_session_rollback(session)

        return self._image_file_id_by_path(file_path=file_path)

    def set_auction_artwork_images(
        self,
        *,
        auction_artwork_id: UUID | object,
        image_paths: Sequence[str] | None,
    ) -> None:
        session = self._get_session()
        normalized_paths = self._normalize_image_paths(image_paths)

        has_image_file = self._table_has_columns(
            "image_file",
            {"image_file_id", "file_path"},
        )
        has_link_table = self._table_has_columns(
            "auction_artwork_image_file",
            {"auction_artwork_id", "image_file_id"},
        )

        if not (has_image_file and has_link_table):
            if self._table_has_columns("auction_artwork", {"img_paths"}):
                session.execute(
                    text(
                        """
                        update auction_artwork
                        set img_paths = :img_paths
                        where auction_artwork_id = :auction_artwork_id
                        """
                    ),
                    {
                        "img_paths": normalized_paths,
                        "auction_artwork_id": auction_artwork_id,
                    },
                )
            return

        desired_ids: list[object] = []
        desired_set: set[object] = set()
        for image_path in normalized_paths:
            image_file_id = self._ensure_image_file_id(file_path=image_path)
            if image_file_id is None or image_file_id in desired_set:
                continue
            desired_set.add(image_file_id)
            desired_ids.append(image_file_id)

        existing_ids = set(
            session.execute(
                text(
                    """
                    select image_file_id
                    from auction_artwork_image_file
                    where auction_artwork_id = :auction_artwork_id
                    """
                ),
                {"auction_artwork_id": auction_artwork_id},
            )
            .scalars()
            .all()
        )

        for image_file_id in existing_ids - desired_set:
            session.execute(
                text(
                    """
                    delete from auction_artwork_image_file
                    where auction_artwork_id = :auction_artwork_id
                      and image_file_id = :image_file_id
                    """
                ),
                {
                    "auction_artwork_id": auction_artwork_id,
                    "image_file_id": image_file_id,
                },
            )

        link_columns = self._get_table_columns("auction_artwork_image_file")
        for image_file_id in desired_ids:
            if image_file_id in existing_ids:
                continue

            if "is_image_matching_processed" in link_columns:
                session.execute(
                    text(
                        """
                        insert into auction_artwork_image_file (
                            auction_artwork_id,
                            image_file_id,
                            is_image_matching_processed
                        )
                        values (:auction_artwork_id, :image_file_id, false)
                        on conflict do nothing
                        """
                    ),
                    {
                        "auction_artwork_id": auction_artwork_id,
                        "image_file_id": image_file_id,
                    },
                )
                continue

            session.execute(
                text(
                    """
                    insert into auction_artwork_image_file (auction_artwork_id, image_file_id)
                    values (:auction_artwork_id, :image_file_id)
                    on conflict do nothing
                    """
                ),
                {
                    "auction_artwork_id": auction_artwork_id,
                    "image_file_id": image_file_id,
                },
            )

    def set_lost_artwork_images(
        self,
        *,
        lost_artwork_id: UUID | object,
        image_paths: Sequence[str] | None,
    ) -> None:
        session = self._get_session()
        normalized_paths = self._normalize_image_paths(image_paths)

        has_image_file = self._table_has_columns(
            "image_file",
            {"image_file_id", "file_path"},
        )
        has_link_table = self._table_has_columns(
            "lost_artwork_image_file",
            {"lost_artwork_id", "image_file_id"},
        )

        if not (has_image_file and has_link_table):
            if self._table_has_columns("lost_artwork", {"img_paths"}):
                session.execute(
                    text(
                        """
                        update lost_artwork
                        set img_paths = :img_paths
                        where lost_artwork_id = :lost_artwork_id
                        """
                    ),
                    {
                        "img_paths": normalized_paths,
                        "lost_artwork_id": lost_artwork_id,
                    },
                )
            return

        desired_ids: list[object] = []
        desired_set: set[object] = set()
        for image_path in normalized_paths:
            image_file_id = self._ensure_image_file_id(file_path=image_path)
            if image_file_id is None or image_file_id in desired_set:
                continue
            desired_set.add(image_file_id)
            desired_ids.append(image_file_id)

        existing_ids = set(
            session.execute(
                text(
                    """
                    select image_file_id
                    from lost_artwork_image_file
                    where lost_artwork_id = :lost_artwork_id
                    """
                ),
                {"lost_artwork_id": lost_artwork_id},
            )
            .scalars()
            .all()
        )

        for image_file_id in existing_ids - desired_set:
            session.execute(
                text(
                    """
                    delete from lost_artwork_image_file
                    where lost_artwork_id = :lost_artwork_id
                      and image_file_id = :image_file_id
                    """
                ),
                {
                    "lost_artwork_id": lost_artwork_id,
                    "image_file_id": image_file_id,
                },
            )

        for image_file_id in desired_ids:
            if image_file_id in existing_ids:
                continue
            session.execute(
                text(
                    """
                    insert into lost_artwork_image_file (lost_artwork_id, image_file_id)
                    values (:lost_artwork_id, :image_file_id)
                    on conflict do nothing
                    """
                ),
                {
                    "lost_artwork_id": lost_artwork_id,
                    "image_file_id": image_file_id,
                },
            )

    def find_auction_artwork_by_lot(
        self,
        lot_id: Optional[str] = None,
        lot_url: Optional[str] = None,
        auction_platform_id: Optional[UUID] = None,
    ) -> Optional[AuctionArtwork]:
        """Find an auction artwork by lot_url (preferred) or lot_id.

        Uses a narrow column load so it remains compatible with live DB schemas
        that may not yet include every generated ORM column.
        """
        if not lot_id and not lot_url:
            return None

        session = self._get_session()
        base_stmt = select(AuctionArtwork).options(
            load_only(
                AuctionArtwork.auction_artwork_id,
                AuctionArtwork.lot_id,
                AuctionArtwork.lot_url,
                AuctionArtwork.title,
                AuctionArtwork.auction_platform_id,
            )
        )

        if auction_platform_id is not None:
            base_stmt = base_stmt.where(
                AuctionArtwork.auction_platform_id == auction_platform_id
            )

        if lot_url:
            by_url = session.execute(
                base_stmt.where(AuctionArtwork.lot_url == lot_url)
            ).scalar_one_or_none()
            if by_url is not None or not lot_id:
                return by_url

        if lot_id:
            return session.execute(
                base_stmt.where(AuctionArtwork.lot_id == lot_id)
            ).scalar_one_or_none()

        return None

    def find_lost_artwork_by_lost_art_id(
        self, lost_art_id: str
    ) -> Optional[LostArtwork]:
        """Find a lost artwork by lost_art_id."""
        session = self._get_session()
        return session.execute(
            select(LostArtwork).where(LostArtwork.lost_art_id == lost_art_id)
        ).scalar_one_or_none()

    def upsert_auction_artwork(
        self, lot_id: Optional[str] = None, lot_url: Optional[str] = None, **kwargs
    ) -> AuctionArtwork:
        """Create or update an auction artwork.

        Uses the connected database's live columns so scrapers keep working even
        if the generated ORM model is ahead of the applied schema.
        """
        legacy_image_paths = kwargs.pop("img_paths", None)

        session = self._get_session()
        actual_columns = self._get_table_columns("auction_artwork")

        values: dict[str, object] = {}
        if lot_id is not None and "lot_id" in actual_columns:
            values["lot_id"] = lot_id
        if lot_url is not None and "lot_url" in actual_columns:
            values["lot_url"] = lot_url
        for key, value in kwargs.items():
            if value is None or key not in actual_columns:
                continue
            values[key] = value

        if not values:
            raise ValueError(
                "upsert_auction_artwork received no columns present in the live table"
            )

        lookup_platform_id = values.get("auction_platform_id")
        if "auction_platform_id" not in actual_columns:
            lookup_platform_id = None

        lookup_candidates: list[tuple[str, object]] = []
        if lot_url is not None and "lot_url" in actual_columns:
            lookup_candidates.append(("lot_url", lot_url))
        if lot_id is not None and "lot_id" in actual_columns:
            lookup_candidates.append(("lot_id", lot_id))

        def _find_existing_id() -> Optional[object]:
            for lookup_column, lookup_value in lookup_candidates:
                lookup_sql = (
                    "select auction_artwork_id from auction_artwork "
                    f"where {lookup_column} = :lookup_value"
                )
                params: dict[str, object] = {"lookup_value": lookup_value}
                if lookup_platform_id is not None:
                    lookup_sql += " and auction_platform_id = :lookup_platform_id"
                    params["lookup_platform_id"] = lookup_platform_id
                lookup_sql += " limit 1"

                existing_id = session.execute(
                    text(lookup_sql), params
                ).scalar_one_or_none()
                if existing_id is not None:
                    return existing_id
            return None

        def _update_existing(existing_id: object) -> SimpleNamespace:
            update_values = {
                key: value
                for key, value in values.items()
                if key != "auction_artwork_id"
            }
            if update_values:
                set_clause = ", ".join(f"{key} = :{key}" for key in update_values)
                params = dict(update_values)
                params["auction_artwork_id"] = existing_id
                session.execute(
                    text(
                        f"update auction_artwork set {set_clause} where auction_artwork_id = :auction_artwork_id"
                    ),
                    params,
                )

            if legacy_image_paths is not None:
                self.set_auction_artwork_images(
                    auction_artwork_id=existing_id,
                    image_paths=legacy_image_paths,
                )

            return SimpleNamespace(
                auction_artwork_id=existing_id,
                lot_id=values.get("lot_id", lot_id),
                lot_url=values.get("lot_url", lot_url),
                title=values.get("title"),
            )

        existing_id = _find_existing_id()
        if existing_id is not None:
            return _update_existing(existing_id)

        insert_columns = list(values.keys())
        columns_sql = ", ".join(insert_columns)
        values_sql = ", ".join(f":{column}" for column in insert_columns)

        savepoint_ctx, has_savepoint = self._savepoint_context(session)
        try:
            with savepoint_ctx:
                inserted_id = session.execute(
                    text(
                        f"insert into auction_artwork ({columns_sql}) values ({values_sql}) returning auction_artwork_id"
                    ),
                    values,
                ).scalar_one()
        except IntegrityError:
            if not has_savepoint:
                self._safe_session_rollback(session)
            existing_id = _find_existing_id()
            if existing_id is None:
                raise
            return _update_existing(existing_id)

        if legacy_image_paths is not None:
            self.set_auction_artwork_images(
                auction_artwork_id=inserted_id,
                image_paths=legacy_image_paths,
            )

        return SimpleNamespace(
            auction_artwork_id=inserted_id,
            lot_id=values.get("lot_id", lot_id),
            lot_url=values.get("lot_url", lot_url),
            title=values.get("title"),
        )

    def upsert_lost_artwork(self, lost_art_id: str, **kwargs) -> LostArtwork:
        """Create or update a lost artwork by lost_art_id."""
        legacy_image_paths = kwargs.pop("img_paths", None)
        existing = self.find_lost_artwork_by_lost_art_id(lost_art_id)

        if existing:
            for key, value in kwargs.items():
                if value is not None:
                    setattr(existing, key, value)
            self._get_session().flush()

            if legacy_image_paths is not None:
                self.set_lost_artwork_images(
                    lost_artwork_id=existing.lost_artwork_id,
                    image_paths=legacy_image_paths,
                )
            return existing

        artwork = LostArtwork(lost_art_id=lost_art_id, **kwargs)
        self.add(artwork)
        self._get_session().flush()

        if legacy_image_paths is not None:
            self.set_lost_artwork_images(
                lost_artwork_id=artwork.lost_artwork_id,
                image_paths=legacy_image_paths,
            )

        return artwork
