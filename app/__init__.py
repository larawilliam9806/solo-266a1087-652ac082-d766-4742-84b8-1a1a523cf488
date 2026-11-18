"""冷链运输温度合规平台 —— Flask 应用工厂与路由。"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask, abort, flash, redirect, render_template, request, url_for,
)

from . import storage
from .engine import analyze_all
from .parser import CSVError, parse_handling_csv, parse_temperature_csv


def create_app(db_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    default_db = Path(__file__).resolve().parent.parent / "data" / "coldchain.db"
    app.config["DATABASE"] = str(db_path or os.environ.get("DATABASE") or default_db)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    app.secret_key = "cold-chain-demo-secret-change-in-production"
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    storage.init_db(app.config["DATABASE"])

    # ---------- 辅助 ----------

    def _read_upload(field: str) -> bytes | None:
        f = request.files.get(field)
        if f is None or f.filename == "":
            return None
        return f.read()

    def _build_draft(trip_id: str, expected_revision: int, name: str,
                     temp_limit: float, temp_bytes: bytes | None,
                     handling_bytes: bytes | None,
                     previous: dict | None) -> dict:
        """合并上传与上一版数据，计算预览。previous 为旧草稿或已确认版本，
        其读数/装卸与 CSV 原文必须来自同一版本（由调用方保证）。"""
        if temp_bytes is not None:
            readings = parse_temperature_csv(temp_bytes)
            temp_csv_text = temp_bytes.decode("utf-8-sig")
        else:
            if not previous:
                raise CSVError("请上传温度 CSV（UTC 时刻、摄氏度）")
            readings = previous["readings"]
            temp_csv_text = previous["temp_csv"]

        if handling_bytes is not None:
            rides = parse_handling_csv(handling_bytes)
            handling_csv_text = handling_bytes.decode("utf-8-sig")
        else:
            if not previous:
                raise CSVError("请上传装卸 CSV（箱号、装车时刻、卸车时刻）")
            rides = [tuple(r) for r in previous["rides"]]
            handling_csv_text = previous["handling_csv"]

        results = analyze_all(readings, rides, temp_limit)
        return {
            "trip_id": trip_id,
            "expected_revision": expected_revision,
            "name": name,
            "temp_limit": temp_limit,
            "readings": readings,
            "rides": rides,
            "results": results,
            "temp_csv": temp_csv_text,
            "handling_csv": handling_csv_text,
        }

    def _save_draft(draft: dict) -> None:
        storage.upsert_draft(
            draft["trip_id"], draft["expected_revision"], draft["name"],
            draft["temp_limit"], draft["temp_csv"], draft["handling_csv"],
            draft["readings"], draft["rides"], draft["results"],
        )

    # ---------- 页面路由 ----------

    @app.route("/")
    def index():
        return render_template("index.html", trips=storage.list_trips())

    @app.route("/trips", methods=["POST"])
    def create_trip():
        trip_id = uuid.uuid4().hex[:12]
        name = request.form.get("name", "").strip() or f"运输趟次 {trip_id[:6]}"
        try:
            temp_limit = float(request.form.get("temp_limit", ""))
        except (TypeError, ValueError):
            flash("请输入有效的温度上限（数字，摄氏度）", "error")
            return redirect(url_for("index"))
        try:
            draft = _build_draft(
                trip_id, expected_revision=0, name=name, temp_limit=temp_limit,
                temp_bytes=_read_upload("temp_file"),
                handling_bytes=_read_upload("handling_file"),
                previous=None,
            )
        except CSVError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        _save_draft(draft)
        return redirect(url_for("preview", trip_id=trip_id))

    @app.route("/trips/<trip_id>/preview")
    def preview(trip_id: str):
        draft = storage.get_draft(trip_id)
        if draft is None:
            abort(404)
        confirmed = None
        try:
            confirmed = storage.get_trip(trip_id)
        except storage.NotFound:
            pass
        stale = confirmed is not None and confirmed["revision"] != draft["expected_revision"]
        return render_template(
            "preview.html", draft=draft, confirmed=confirmed, stale=stale,
            fmt=fmt_ts, fmt_duration=fmt_duration,
        )

    @app.route("/trips/<trip_id>/revise", methods=["POST"])
    def revise(trip_id: str):
        try:
            confirmed = storage.get_trip(trip_id)
        except storage.NotFound:
            confirmed = None
        current = storage.get_draft(trip_id)
        if current is None and confirmed is None:
            abort(404)
        # 基准修订号以已确认版本为准；草稿始终针对最新确认版。
        # 沿用数据优先取当前草稿（其读数/装卸与 CSV 原文同属一次修订），
        # 没有草稿才回退到已确认版本，绝不把两个版本的数据拼在一起。
        expected = confirmed["revision"] if confirmed is not None else 0
        if current is not None:
            previous = current
        else:
            previous = {
                "readings": confirmed["readings"],
                "rides": [(r["box"], r["loaded_at"], r["unloaded_at"]) for r in confirmed["rides"]],
                "temp_csv": confirmed["temp_csv"],
                "handling_csv": confirmed["handling_csv"],
            }

        name = request.form.get("name", "").strip() or (
            current["name"] if current else confirmed["name"]
        )
        fallback_limit = current["temp_limit"] if current else confirmed["temp_limit"]
        # 出错时回到用户实际所在的页面：有草稿回预览页，否则回正式结论页
        back_url = url_for("preview", trip_id=trip_id) if current else url_for(
            "trip_detail", trip_id=trip_id)
        try:
            temp_limit = float(request.form.get("temp_limit", fallback_limit))
        except (TypeError, ValueError):
            flash("温度上限不是有效数字", "error")
            return redirect(back_url)
        try:
            draft = _build_draft(
                trip_id, expected_revision=expected, name=name, temp_limit=temp_limit,
                temp_bytes=_read_upload("temp_file"),
                handling_bytes=_read_upload("handling_file"),
                previous=previous,
            )
        except CSVError as exc:
            flash(str(exc), "error")
            return redirect(back_url)
        try:
            _save_draft(draft)
        except storage.RevisionConflict as exc:
            # 与并发确认交错：正式版已前进，明确拒绝，不写下即过期的草稿
            flash(f"修订未保存：{exc}", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
        flash("已按修正内容重新计算预览，尚未写入正式结论", "info")
        return redirect(url_for("preview", trip_id=trip_id))

    @app.route("/trips/<trip_id>/confirm", methods=["POST"])
    def confirm(trip_id: str):
        token = request.form.get("draft_id", "")
        draft = storage.get_draft(trip_id)
        if draft is None:
            try:
                storage.get_trip(trip_id)
            except storage.NotFound:
                abort(404)
            flash("没有待确认的预览：草稿已被确认或替换，请查看当前正式版本", "error")
            return redirect(url_for("trip_detail", trip_id=trip_id))
        if not token or token != draft["draft_id"]:
            # 页面之后出现了新修订：旧页确认必须过期，正式版保持原状
            flash(
                "确认被拒绝：该预览页已被更新的修订替换（页面已过期），"
                "正式版本未做任何改动。请核对最新预览后再确认。",
                "error",
            )
            return redirect(url_for("preview", trip_id=trip_id))
        try:
            new_rev = storage.commit_revision(
                trip_id, draft["expected_revision"], draft["name"], draft["temp_limit"],
                draft["readings"], [tuple(r) for r in draft["rides"]], draft["results"],
                temp_csv=draft["temp_csv"], handling_csv=draft["handling_csv"],
                draft_token=token,
            )
        except storage.DraftConflict as exc:
            flash(f"确认失败：{exc}。正式版本未改动，请核对最新预览。", "error")
            return redirect(url_for("preview", trip_id=trip_id))
        except storage.RevisionConflict as exc:
            flash(f"确认失败：{exc}。请基于最新版本重新修正。", "error")
            return redirect(url_for("preview", trip_id=trip_id))
        flash(f"已确认，正式版本号 rev {new_rev}（整趟替换，旧结论已清除）", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trips/<trip_id>")
    def trip_detail(trip_id: str):
        trip = storage.get_trip(trip_id)
        has_draft = storage.get_draft(trip_id) is not None
        return render_template(
            "detail.html", trip=trip, has_draft=has_draft,
            fmt=fmt_ts, fmt_duration=fmt_duration,
        )

    @app.route("/trips/<trip_id>/api/results")
    def api_results(trip_id: str):
        trip = storage.get_trip(trip_id)
        return app.response_class(
            json.dumps(trip, ensure_ascii=False, indent=2),
            mimetype="application/json",
        )

    @app.errorhandler(404)
    def not_found(_):
        return render_template("404.html"), 404

    @app.errorhandler(storage.NotFound)
    def trip_not_found(_):
        return render_template("404.html"), 404

    @app.template_filter("fmt")
    def fmt_ts(epoch: int) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    @app.template_filter("duration")
    def fmt_duration(seconds: int) -> str:
        sign = "-" if seconds < 0 else ""
        seconds = abs(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{sign}{h:d}:{m:02d}:{s:02d}"

    return app
