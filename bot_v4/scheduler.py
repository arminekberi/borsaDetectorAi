"""İş zamanlayıcı. Yeniden başlatmada aynı iş ikinci kez çalışmaz, eski işler de geç tetiklenmez."""

from __future__ import annotations

import logging
import time as clock
from datetime import datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from bot_v4 import session
from bot_v4.engine import Engine
from bot_v4.market import StaleData
from bot_v4.settings import BotConfig

logger = logging.getLogger("bot_v4.scheduler")
TZ = ZoneInfo("Europe/Istanbul")
SCAN_WINDOW = timedelta(minutes=30)
CLOSE_WINDOW = timedelta(hours=3)
RETRY_GAP = timedelta(minutes=10)


def parse_time(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


class Scheduler:
    def __init__(self, cfg: BotConfig, engine: Engine):
        self.cfg = cfg
        self.engine = engine
        self.store = engine.store
        self._last_close_try = None
        self._warned = set()

    def _run(self, job: str, now: datetime, action: Callable[[datetime], None]) -> None:
        try:
            action(now)
            self.store.mark_job(job, now)
        except Exception:
            logger.exception("%s başarısız", job)
            self._warn(f"{job}:{now:%Y-%m-%d}", f"⚠️ {job} işi başarısız oldu, kayıtları kontrol et.")
            self.store.mark_job(job, now)

    def _warn(self, key: str, text: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self.engine.send(text)

    def tick(self, now: datetime) -> None:
        day = now.date()
        if not session.calendar_known(day):
            self._warn(f"calendar:{day.year}", f"⚠️ {day.year} BIST tatil takvimi eklenmemiş. config/bist_calendar.py güncellenmeli.")
        if day.weekday() == 6:
            if not self.cfg.sunday_review:
                return  # SUNDAY_REVIEW=false (varsayilan): Pazar isi hic calismaz
            due = datetime.combine(day, parse_time(self.cfg.sunday_time), tzinfo=now.tzinfo)
            job = f"sunday:{day}"
            if due <= now <= due + timedelta(hours=3) and not self.store.job_done(job):
                self._run(job, now, self.engine.sunday_job)
            return
        if not session.is_trading_day(day):
            return
        close_at = session.session_close(day)
        for n, text in enumerate(self.cfg.scan_times, start=1):
            at = parse_time(text)
            due = datetime.combine(day, at, tzinfo=now.tzinfo)
            job = f"scan:{day}:{text}"
            # Yarim gunde seans sonrasina dusen sorgu atlanir
            if at < close_at and due <= now <= due + SCAN_WINDOW and not self.store.job_done(job):
                slot = f"S{n}"
                self._run(job, now, lambda moment, s=slot: self.engine.scan_intraday(moment, s))

        monitor_end = (datetime.combine(day, close_at) + timedelta(minutes=10)).time()
        if session.SESSION_OPEN <= now.time() <= monitor_end:
            job = f"monitor:{day}:{now.hour:02d}:{now.minute // 15}"
            if not self.store.job_done(job):
                self._run(job, now, self.engine.monitor)

        close_text = self.cfg.half_day_close_time if session.is_half_day(day) else self.cfg.close_time
        due = datetime.combine(day, parse_time(close_text), tzinfo=now.tzinfo)
        job = f"close:{day}"
        if due <= now <= due + CLOSE_WINDOW and not self.store.job_done(job):
            if self._last_close_try is None or now - self._last_close_try >= RETRY_GAP:
                self._last_close_try = now
                try:
                    self.engine.close_job(now)
                    self.store.mark_job(job, now)
                except StaleData:
                    logger.info("Günlük veri henüz gelmedi, tekrar denenecek")
                except Exception:
                    logger.exception("Kapanış işi başarısız")
                    self._warn(f"close:{day}", "⚠️ Kapanış işi hata verdi, tekrar denenecek.")
        elif now > due + CLOSE_WINDOW and not self.store.job_done(job):
            self._warn(f"close-missed:{day}", "⚠️ Bugünün kapanış verisi alınamadı. Skorlar güncellenmedi.")
            self.store.mark_job(job, now)

    def run_forever(self, pause: float = 20.0) -> None:
        logger.info("Zamanlayıcı başladı")
        while True:
            try:
                self.tick(datetime.now(TZ))
            except Exception:
                logger.exception("Zamanlayıcı hatası")
            clock.sleep(pause)
