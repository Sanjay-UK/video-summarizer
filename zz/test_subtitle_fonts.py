"""Subtitle text in Indic scripts: fonts ship with the worker, and missing glyphs fail instead of showing boxes.

The rendering tests draw real frames with an FFmpeg build that has libass, and check libass's own report of glyph
coverage. They skip when no such FFmpeg is available. The other tests check the wiring that makes rendering work.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

from src import highlight_assembler as ha  # noqa: E402
from src import highlight_ffmpeg as hf  # noqa: E402
from src.locales import FONTS_DIR, LOCALES  # noqa: E402

SAMPLES = {
    "Noto Sans Devanagari": "हिंदी में उपशीर्षक परीक्षण",
    "Noto Sans Kannada": "ಕನ್ನಡ ಉಪಶೀರ್ಷಿಕೆ ಪರೀಕ್ಷೆ",
    "Noto Sans Tamil": "தமிழ் வசன சோதனை",
    "Noto Sans Telugu": "తెలుగు ఉపశీర్షిక పరీక్ష",
    "DejaVu Sans": "English subtitle check",
}


def _family_name(path: Path) -> str:
    """The family name (name ID 1) stored inside a TrueType file. libass matches ASS styles against this name."""
    data = path.read_bytes()
    num_tables = int.from_bytes(data[4:6], "big")
    for i in range(num_tables):
        record = 12 + 16 * i
        if data[record:record + 4] != b"name":
            continue
        table = int.from_bytes(data[record + 8:record + 12], "big")
        count = int.from_bytes(data[table + 2:table + 4], "big")
        string_base = table + int.from_bytes(data[table + 4:table + 6], "big")
        for r in range(count):
            rec = table + 6 + 12 * r
            platform, name_id = int.from_bytes(data[rec:rec + 2], "big"), int.from_bytes(data[rec + 6:rec + 8], "big")
            length = int.from_bytes(data[rec + 8:rec + 10], "big")
            offset = int.from_bytes(data[rec + 10:rec + 12], "big")
            if name_id != 1:
                continue
            raw = data[string_base + offset:string_base + offset + length]
            # Platform 1 (Mac) is single-byte text; the Unicode and Windows platforms are UTF-16 big-endian.
            return raw.decode("latin-1" if platform == 1 else "utf-16-be")
    raise ValueError(f"no family name in {path}")


def _ass(samples):
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1280", "PlayResY: 360", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
    ]
    for i, (font, _text) in enumerate(samples):
        lines.append(f"Style: S{i},{font},56,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,60,1")
    lines += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for i, (_font, text) in enumerate(samples):
        lines.append(f"Dialogue: 0,0:00:0{i}.00,0:00:0{i + 1}.00,S{i},,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def _libass_ffmpeg():
    try:
        binary = hf.resolve_ffmpeg_bin()
    except Exception:  # noqa: BLE001
        return None
    proc = subprocess.run([binary, "-hide_banner", "-filters"], capture_output=True, text=True)
    return binary if " subtitles " in proc.stdout else None


class WiringTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "runpod_worker" / "Dockerfile").exists(), "Dockerfile is not in the worker image")
    def test_worker_image_copies_the_bundled_fonts_and_registers_them(self):
        dockerfile = (ROOT / "runpod_worker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY static/fonts /app/static/fonts", dockerfile)
        self.assertIn("fc-cache -f", dockerfile)

    def test_every_caption_language_has_its_font_file_in_the_repo(self):
        for code, spec in LOCALES.items():
            with self.subTest(language=code):
                self.assertTrue(Path(spec.caption_font_file).is_file(), spec.caption_font_file)

    def test_every_caption_style_names_the_family_its_bundled_file_declares(self):
        # libass looks a style up by the family name inside the file. A style naming a different family silently
        # falls back to a default face, which can lack the script and draw boxes.
        for code, spec in LOCALES.items():
            with self.subTest(language=code):
                self.assertEqual(_family_name(Path(spec.caption_font_file)), spec.caption_font_name)

    def test_missing_glyph_report_is_detected_in_ffmpeg_stderr(self):
        report = "[Parsed_subtitles_0 @ 0x1] fontselect: failed to find any fallback with glyph 0x902 for font"
        self.assertTrue(ha.subtitle_glyphs_missing(report))
        self.assertFalse(ha.subtitle_glyphs_missing("frame=1 fps=0.0 size=N/A"))
        self.assertFalse(ha.subtitle_glyphs_missing(""))

    def test_scene_muxing_fails_clearly_when_glyphs_are_missing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name)
        ass = work / "caption.ass"
        ass.write_text(_ass([("Noto Sans Kannada", "ಕನ್ನಡ")]), encoding="utf-8")
        report = "fontselect: failed to find any fallback with glyph 0xc95 for font: (Noto Sans Kannada)"
        with mock.patch.object(ha, "run_ffmpeg", return_value=report):
            with self.assertRaises(ha.SubtitleFontError) as ctx:
                ha.mux_scene(work / "v.mp4", work / "a.mp3", ass, 2.0, work / "out.mp4")
        self.assertIn("static/fonts", str(ctx.exception))

    def test_scene_muxing_succeeds_when_no_glyphs_are_missing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name)
        ass = work / "caption.ass"
        ass.write_text(_ass([("DejaVu Sans", "Hello")]), encoding="utf-8")
        with mock.patch.object(ha, "run_ffmpeg", return_value=""):
            self.assertEqual(ha.mux_scene(work / "v.mp4", work / "a.mp3", ass, 2.0, work / "out.mp4"),
                             work / "out.mp4")


@unittest.skipIf(_libass_ffmpeg() is None, "no FFmpeg build with libass (subtitles filter) on this machine")
class RenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = _libass_ffmpeg()

    def _render(self, fonts_dir: Path, samples, system_fonts: bool = True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        work = Path(tmp.name)
        ass = work / "sample.ass"
        ass.write_text(_ass(samples), encoding="utf-8")
        env = dict(os.environ)
        if system_fonts:
            # fontconfig must know the bundled fonts, as the worker image does after `fc-cache`, so libass can
            # fall back to them for mixed-script lines. On macOS libass uses CoreText and ignores this file.
            conf = work / "fonts.conf"
            conf.write_text(
                '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
                '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>'
                f"<dir>{FONTS_DIR}</dir><cachedir>{work / 'cache'}</cachedir></fontconfig>",
                encoding="utf-8",
            )
            env["FONTCONFIG_FILE"] = str(conf)
        if not system_fonts:
            # Like the worker image: fontconfig knows no fonts at all, so nothing can cover the Indic scripts.
            conf = work / "fonts.conf"
            conf.write_text('<?xml version="1.0"?><fontconfig><dir>/nonexistent-fonts</dir>'
                            '<cachedir>' + str(work / "cache") + '</cachedir></fontconfig>', encoding="utf-8")
            env["FONTCONFIG_FILE"] = str(conf)
        proc = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
             "-f", "lavfi", "-i", "color=c=black:s=1280x360:d=5",
             "-vf", f"subtitles=filename={ass}:fontsdir={fonts_dir}",
             "-ss", "0.5", "-frames:v", "1", str(work / "frame.png")],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return work / "frame.png", proc.stderr

    def test_bundled_fonts_render_every_script_without_missing_glyphs(self):
        for font, text in SAMPLES.items():
            with self.subTest(font=font):
                frame, stderr = self._render(FONTS_DIR, [(font, text)])
                self.assertFalse(ha.subtitle_glyphs_missing(stderr), stderr)
                self.assertGreater(frame.stat().st_size, 3000, "a rendered line should add visible pixels")

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "macOS uses CoreText for libass and ignores fontconfig; reproduce this on Linux (worker image)")
    def test_without_fonts_the_missing_glyphs_are_reported(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        _frame, stderr = self._render(empty, [("Noto Sans Devanagari", SAMPLES["Noto Sans Devanagari"])],
                                      system_fonts=False)
        self.assertTrue(ha.subtitle_glyphs_missing(stderr), "the worker failure mode must be detectable")

    def test_mixed_script_line_renders_when_all_scripts_are_bundled(self):
        mixed = "English हिंदी ಕನ್ನಡ தமிழ் తెలుగు"
        frame, stderr = self._render(FONTS_DIR, [("DejaVu Sans", mixed)])
        self.assertFalse(ha.subtitle_glyphs_missing(stderr), stderr)

    def test_production_filter_renders_each_language_from_a_project_path_with_spaces(self):
        # Uses the same filter string as mux_scene, so the escaping of the fonts path is what gets tested. The fonts
        # are copied to a directory whose name has a space and parentheses (the project path has a space on macOS),
        # and the production code is pointed at that copy, so the test does not depend on where the repo lives.
        spaced = Path(tempfile.mkdtemp(prefix="fonts with space (x) "))
        self.addCleanup(shutil.rmtree, spaced, True)
        for font in FONTS_DIR.glob("*.ttf"):
            shutil.copy2(font, spaced / font.name)
        for code, spec in LOCALES.items():
            with self.subTest(language=code), mock.patch.object(ha, "FONTS_DIR", spaced):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                work = Path(tmp.name)
                ass = work / "caption.ass"
                ass.write_text(_ass([(spec.caption_font_name, SAMPLES[spec.caption_font_name])]), encoding="utf-8")
                vf = ha._subtitles_filter(ass)
                self.assertIn("fontsdir=", vf)
                proc = subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
                     "-f", "lavfi", "-i", "color=c=black:s=1280x360:d=5",
                     "-vf", vf, "-frames:v", "1", str(work / "frame.png")],
                    capture_output=True, text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(ha.subtitle_glyphs_missing(proc.stderr), proc.stderr)


if __name__ == "__main__":
    unittest.main()
