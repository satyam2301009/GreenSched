
from sqlalchemy import String, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.db import Base, engine
from app import models

Base.metadata.create_all(bind=engine)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    task_type: Mapped[str] = mapped_column(String(30))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    deadline: Mapped[str] = mapped_column(String(50))
    description: Mapped[str] = mapped_column(Text, default="")
    script_code: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[str] = mapped_column(String(50))
    scheduled_start: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    scheduled_finish: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    carbon_intensity: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
