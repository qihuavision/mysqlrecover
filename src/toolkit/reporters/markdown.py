"""Markdown 报告生成（FP-06, Sprint 3）。

用 Jinja2 模板渲染演练结果，输出到本地目录。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from toolkit.core.logger import get_logger

logger = get_logger(__name__)

# 内置默认模板目录（随包分发）
_DEFAULT_TEMPLATE_DIR = Path(__file__).parent / "templates"


class MarkdownReporter:
    """Markdown 报告生成器。"""

    def __init__(self, output_dir: str, template_dir: str | None = None):
        template_path = Path(template_dir) if template_dir else _DEFAULT_TEMPLATE_DIR
        self.env = Environment(
            loader=FileSystemLoader(str(template_path)),
            autoescape=select_autoescape(disabled_extensions=("j2",)),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(self, result, archive_root: str = "", operator: str = "toolkit") -> Path:
        """渲染报告到 output_dir，返回文件路径。

        Args:
            result: Orchestrator 产出的 DrillResult
            archive_root: 审计日志根目录（写入报告供查阅）
            operator: 执行人标识
        """
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        date_str = datetime.now().strftime("%Y-%m-%d")
        total_attempted = result.success + result.failed
        success_rate = (result.success / total_attempted * 100) if total_attempted else 0.0

        template = self.env.get_template("report.md.j2")
        content = template.render(
            run_id=result.run_id,
            duration_sec=result.duration_sec,
            generated_at=generated_at,
            date=date_str,
            target_host=getattr(result, "target_host", ""),
            operator=operator,
            total=result.total,
            success=result.success,
            failed=result.failed,
            retried=result.retried,
            skipped=getattr(result, "skipped", 0),
            success_rate=success_rate,
            archive_root=archive_root,
            tasks=result.task_results,
        )

        out_file = self.output_dir / f"drill-run{result.run_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
        out_file.write_text(content, encoding="utf-8")
        logger.info("报告已生成: %s", out_file)
        return out_file

    @staticmethod
    def summarize_markdown(result) -> str:
        """生成简短摘要（供企业微信通知用）。"""
        total_attempted = result.success + result.failed
        rate = f"{result.success / total_attempted * 100:.1f}%" if total_attempted else "N/A"
        lines = [
            f"### {'✅' if result.failed == 0 else '❌'} MySQL 恢复演练{'完成' if result.failed == 0 else '（有失败）'}",
            "",
            f"**批次**：#{result.run_id}",
            f"**结果**：{result.success}/{total_attempted} 成功（{rate}）",
            f"**失败**：{result.failed}　**重试**：{result.retried}",
            f"**耗时**：{result.duration_sec}s",
        ]
        # 失败实例明细（最多 5 个）
        failures = [t for t in result.task_results if t.get("status") == "FAILED_FINAL"]
        if failures:
            lines.append("")
            lines.append("**失败实例**：")
            for t in failures[:5]:
                err = (t.get("error") or "")[:50]
                lines.append(f"- ❌ `{t.get('instance')}`（{t.get('version')}）{err}")
            if len(failures) > 5:
                lines.append(f"- 等共 {len(failures)} 个失败，详见报告")
        return "\n".join(lines)
