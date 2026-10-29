"""生成 README 用的日间 / 夜间 SVG，配色与麦麦主仓库 README 保持一致。

运行：python docs/readme/build.py
"""

from pathlib import Path

OUT = Path(__file__).resolve().parent
FONT = '"Microsoft YaHei UI","PingFang SC","Noto Sans SC","Microsoft YaHei",sans-serif'
MINT, SUN, CORAL = "#8fd6a0", "#ffd84a", "#f08a5d"

THEMES = {
    "day": dict(
        frame="#ddd4bf", shadow="#9c927b", hi="#f6f1e3", lo="#bdb39c", line="#5a544a", vent="#a89e87",
        screen="#fbf9f0", window="#ebe4d2", ink="#3a3833", sub="#66625a", label="#5f5849",
        tag_bg="#5a544a", tag_fg="#f3eee1", led=MINT, dots="rgba(0,0,0,.05)", paper="#fffdf6",
        glass="#f6f1e3", liquid=MINT,
    ),
    "night": dict(
        frame="#2f2c28", shadow="#1d1b18", hi="#4a453f", lo="#2a2723", line="#171513", vent="#1d1b18",
        screen="#1e221e", window="#3a3631", ink="#e8e2cf", sub="#a8a290", label="#b5ad9a",
        tag_bg="#ebe4d2", tag_fg="#2f2c28", led="#4fd07a", dots="rgba(255,255,255,.04)", paper="#e8e2cf",
        glass="#2a2e2a", liquid="#6fbf86",
    ),
}

# 5×7 像素字，只收录用到的字符。
GLYPHS = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01111"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "0": ["01110", "10011", "10101", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00110", "01000", "10000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "+": ["00000", "00100", "00100", "11111", "00100", "00100", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "·": ["00000", "00000", "00000", "01100", "01100", "00000", "00000"],
    " ": ["00000"] * 7,
}


# 粗体时直接加宽会糊掉 1 像素缝隙的字母，单独画一份。
BOLD_GLYPHS = {
    "M": ["1100011", "1110111", "1111111", "1101011", "1100011", "1100011", "1100011"],
    "W": ["1100011", "1100011", "1100011", "1101011", "1111111", "1110111", "1100011"],
    "N": ["110011", "111011", "111011", "110111", "110111", "110011", "110011"],
    "G": ["011111", "110000", "110000", "110111", "110011", "110011", "011111"],
    "K": ["110011", "110110", "111100", "111000", "111100", "110110", "110011"],
    "R": ["111110", "110011", "110011", "111110", "110110", "110011", "110011"],
    "V": ["110011", "110011", "110011", "110011", "110011", "011110", "001100"],
    "X": ["110011", "110011", "011110", "001100", "011110", "110011", "110011"],
    "Y": ["110011", "110011", "011110", "001100", "001100", "001100", "001100"],
    "0": ["011110", "110011", "110011", "110011", "110011", "110011", "011110"],
    "4": ["000110", "001110", "011110", "110110", "111111", "000110", "000110"],
}


def _glyph(char: str, bold: bool) -> list:
    if not bold:
        return GLYPHS[char]
    if char in BOLD_GLYPHS:
        return BOLD_GLYPHS[char]
    # 其余字母的竖笔之间至少隔两格，把每个像素向右加宽一格即可。
    return ["".join("1" if "1" in (bits + "0")[max(0, i - 1):i + 1] else "0" for i in range(6)) for bits in GLYPHS[char]]


def pixel_width(text: str, px: float, tracking: float = 1, bold: bool = False) -> float:
    widths = [len(_glyph(char, bold)[0]) + tracking for char in text.upper()]
    return (sum(widths) - tracking) * px


def pixel_text(
    text: str, x: float, y: float, px: float, fill: str, tracking: float = 1, bold: bool = False,
) -> str:
    """以左上角为原点绘制像素字，合并成一条 path 减小体积。"""
    parts = []
    ox = x
    for char in text.upper():
        glyph = _glyph(char, bold)
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    parts.append(f"M{ox + col * px:.2f} {y + row * px:.2f}h{px:.2f}v{px:.2f}h{-px:.2f}z")
        ox += (len(glyph[0]) + tracking) * px
    return f'<path d="{"".join(parts)}" fill="{fill}"/>'


def svg(width: int, height: int, label: str, body: str, t: dict, vent_height: int = 18) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
        f'role="img" aria-label="{label}"><defs><style>.c{{font-family:{FONT};font-weight:700}}</style>'
        f'<pattern id="dots" width="4" height="4" patternUnits="userSpaceOnUse">'
        f'<rect width="1" height="1" fill="{t["dots"]}"/></pattern>'
        f'<pattern id="vent" width="6" height="{vent_height}" patternUnits="userSpaceOnUse">'
        f'<rect width="2" height="{vent_height}" fill="{t["vent"]}"/></pattern></defs>{body}</svg>\n'
    )


def bevel(x: float, y: float, w: float, h: float, t: dict, drop: float = 6) -> str:
    """麦麦 README 的机壳面板：硬投影、描边和上亮下暗的倒角。"""
    return (
        f'<rect x="{x}" y="{y + drop}" width="{w}" height="{h}" fill="{t["shadow"]}"/>'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{t["frame"]}" stroke="{t["line"]}" stroke-width="2"/>'
        f'<path d="M{x + 3} {y + h - 2}V{y + 3}H{x + w - 3}" fill="none" stroke="{t["hi"]}" stroke-width="2"/>'
        f'<path d="M{x + w - 3} {y + 3}V{y + h - 3}H{x + 3}" fill="none" stroke="{t["lo"]}" stroke-width="2"/>'
    )


def tri_stripe(x: float, y: float, w: float, h: float) -> str:
    a, b = w * 0.6, w * 0.16
    return (
        f'<rect x="{x}" y="{y}" width="{a:.1f}" height="{h}" fill="{MINT}"/>'
        f'<rect x="{x + a:.1f}" y="{y}" width="{b:.1f}" height="{h}" fill="{SUN}"/>'
        f'<rect x="{x + a + b:.1f}" y="{y}" width="{w - a - b:.1f}" height="{h}" fill="{CORAL}"/>'
    )


def bubble(x: float, y: float, w: float, h: float, fill: str, t: dict, tail_left: bool = True) -> str:
    tail = (f"M{x + 10} {y + h}l-4 9l13 -9" if tail_left else f"M{x + w - 10} {y + h}l4 9l-13 -9")
    dots = "".join(
        f'<rect x="{x + w / 2 - 11 + i * 9}" y="{y + h / 2 - 2}" width="4" height="4" fill="{t["line"]}"/>'
        for i in range(3)
    )
    return (
        f'<path d="{tail}z" fill="{fill}" stroke="{t["line"]}" stroke-width="2" stroke-linejoin="round"/>'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{t["line"]}" stroke-width="2"/>'
        f'<path d="M{x + 10} {y + h}h9" stroke="{fill}" stroke-width="3"/>{dots}'
    )


def illustration(t: dict) -> str:
    """右侧小窗：三条群聊气泡滴进长着新芽的烧瓶，瓶底酿出一页 TXT。"""
    line = t["line"]
    parts = [
        f'<rect x="574" y="60" width="266" height="250" fill="{t["window"]}" stroke="{line}" stroke-width="2"/>',
        '<rect x="575" y="61" width="264" height="248" fill="url(#dots)"/>',
        bubble(592, 82, 64, 30, MINT, t),
        bubble(760, 92, 60, 28, SUN, t, tail_left=False),
        bubble(606, 140, 52, 26, CORAL, t),
        # 气泡落入瓶口的虚线轨迹。
        f'<path d="M640 120 Q 670 128 694 150" fill="none" stroke="{line}" stroke-width="2" stroke-dasharray="4 5"/>',
        f'<path d="M772 128 Q 744 134 722 152" fill="none" stroke="{line}" stroke-width="2" stroke-dasharray="4 5"/>',
        f'<path d="M660 160 Q 680 160 696 166" fill="none" stroke="{line}" stroke-width="2" stroke-dasharray="4 5"/>',
        # 烧瓶。
        f'<path d="M696 146h24v40l38 58a14 14 0 0 1 -12 22h-76a14 14 0 0 1 -12 -22l38 -58z" '
        f'fill="{t["glass"]}" stroke="{line}" stroke-width="2.5" stroke-linejoin="round"/>',
        f'<path d="M680 214h56l22 30a14 14 0 0 1 -12 22h-76a14 14 0 0 1 -12 -22z" fill="{t["liquid"]}"/>',
        f'<path d="M696 146h24v40l38 58a14 14 0 0 1 -12 22h-76a14 14 0 0 1 -12 -22l38 -58z" '
        f'fill="none" stroke="{line}" stroke-width="2.5" stroke-linejoin="round"/>',
        f'<rect x="690" y="140" width="36" height="9" rx="2" fill="{t["frame"]}" stroke="{line}" stroke-width="2"/>',
        f'<rect x="686" y="232" width="7" height="7" fill="{t["paper"]}" opacity=".85"/>',
        f'<rect x="714" y="222" width="5" height="5" fill="{t["paper"]}" opacity=".85"/>',
        f'<rect x="728" y="244" width="6" height="6" fill="{t["paper"]}" opacity=".85"/>',
        f'<path d="M681 196l4 -4" stroke="{t["paper"]}" stroke-width="3" opacity=".7"/>',
        # 瓶口的新芽，呼应麦麦的萝卜叶。
        f'<path d="M708 140v-14" stroke="{line}" stroke-width="2.5"/>',
        f'<path d="M708 128c-4 -12 -18 -14 -22 -8c4 8 14 10 22 8z" fill="{MINT}" stroke="{line}" stroke-width="2" stroke-linejoin="round"/>',
        f'<path d="M708 124c4 -12 18 -14 22 -8c-4 8 -14 10 -22 8z" fill="{MINT}" stroke="{line}" stroke-width="2" stroke-linejoin="round"/>',
        # 像素小爱心。
        pixel_heart(742, 170, 3, CORAL),
        # 酿出的 TXT 纸条。
        f'<g transform="rotate(-8 790 262)">'
        f'<rect x="764" y="232" width="56" height="62" fill="{t["paper"]}" stroke="{line}" stroke-width="2"/>'
        f'<path d="M806 232v12h14" fill="{t["window"]}" stroke="{line}" stroke-width="2"/>'
        + pixel_text("TXT", 771, 246, 2, CORAL, tracking=0.8, bold=True)
        + "".join(f'<rect x="772" y="{266 + i * 7}" width="{40 - (i % 2) * 12}" height="3" fill="{t["lo"]}"/>' for i in range(4))
        + "</g>",
        f'<path d="M760 262 q 6 -2 8 -8" fill="none" stroke="{line}" stroke-width="2" stroke-dasharray="3 4"/>',
    ]
    return "".join(parts)


def pixel_heart(x: float, y: float, px: float, fill: str) -> str:
    rows = ["0110110", "1111111", "1111111", "0111110", "0011100", "0001000"]
    d = "".join(
        f"M{x + c * px} {y + r * px}h{px}v{px}h{-px}z"
        for r, bits in enumerate(rows) for c, bit in enumerate(bits) if bit == "1"
    )
    return f'<path d="{d}" fill="{fill}"/>'


BAR = "MAIBOT PLUGIN // MEMBER DISTILLER"
VERSION = "V1.0.0"


def hero(t: dict) -> str:
    line = t["line"]
    title = "DISTILLER"
    px = 7.2
    body = [
        bevel(1, 1, 858, 326, t),
        f'<line x1="2" y1="42" x2="858" y2="42" stroke="{line}" stroke-width="2"/>',
        f'<g stroke="{line}" stroke-width="1.5"><rect x="16" y="12" width="12" height="20" fill="{MINT}"/>'
        f'<rect x="28" y="12" width="12" height="20" fill="{SUN}"/><rect x="40" y="12" width="12" height="20" fill="{CORAL}"/></g>',
        pixel_text(BAR, 66, 15, 2, t["label"], tracking=1.2, bold=True),
        f'<rect x="{66 + pixel_width(BAR, 2, 1.2, True) + 22:.0f}" y="13" '
        f'width="{828 - 66 - pixel_width(BAR, 2, 1.2, True) - 34:.0f}" height="18" fill="url(#vent)"/>',
        f'<rect x="828" y="15" width="14" height="14" fill="{t["led"]}" stroke="{line}" stroke-width="1.5"/>',
        # 左侧屏幕。
        f'<rect x="18" y="60" width="538" height="250" fill="{t["screen"]}" stroke="{line}" stroke-width="2"/>',
        '<rect x="19" y="61" width="536" height="248" fill="url(#dots)"/>',
        f'<path d="M21 309V63H555" fill="none" stroke="rgba(0,0,0,.08)" stroke-width="4"/>',
        f'<rect x="42" y="84" width="10" height="10" fill="{CORAL}"/>',
        f'<text class="c" x="62" y="95" font-size="15" letter-spacing="3" fill="{t["sub"]}">群友蒸馏</text>',
        pixel_text("MEMBER", 146, 84, 1.6, t["sub"], tracking=1.2, bold=True),
        pixel_text(VERSION, 532 - pixel_width(VERSION, 1.6, 1.2, True), 84, 1.6, t["lo"], tracking=1.2, bold=True),
        pixel_text(title, 42, 116, px, t["ink"], tracking=0.8, bold=True),
        tri_stripe(42, 182, pixel_width(title, px, 0.8, bold=True), 5),
        f'<text class="c" x="42" y="222" font-size="20" fill="{t["ink"]}">把群友说过的话，酿成麦麦的说话方式</text>',
        pixel_text("COLLECT · EXPORT · DISTILL", 42, 236, 1.8, t["sub"], tracking=1.2, bold=True),
        f'<rect x="42" y="266" width="118" height="22" fill="{t["tag_bg"]}"/>',
        pixel_text("SDK 2.12+", 52, 272, 1.6, t["tag_fg"], tracking=1.2, bold=True),
        f'<rect x="168" y="267" width="62" height="20" fill="none" stroke="{line}" stroke-width="1.5"/>',
        pixel_text("MIT", 182, 272, 1.6, t["ink"], tracking=1.2, bold=True),
        f'<rect x="238" y="267" width="96" height="20" fill="none" stroke="{line}" stroke-width="1.5"/>',
        pixel_text("NO LLM", 249, 272, 1.6, t["ink"], tracking=1.2, bold=True),
        illustration(t),
    ]
    return svg(860, 334, "麦麦插件 · 群友蒸馏：把群友说过的话，酿成麦麦的说话方式", "".join(body), t)


SECTIONS = {
    "intro": ("INTRO", "简介", MINT),
    "flow": ("FLOW", "工作流程", SUN),
    "setup": ("SETUP", "安装与配置", MINT),
    "usage": ("USAGE", "指令", CORAL),
    "persona": ("PERSONA", "酿成人设", SUN),
    "privacy": ("PRIVACY", "数据与隐私", CORAL),
}


def section(key: str, t: dict) -> str:
    tag, title, led = SECTIONS[key]
    tag_width = pixel_width(tag, 2, 1.5, True) + 34
    title_width = len(title) * 18 + 30
    vent_x = tag_width + title_width
    body = (
        bevel(1, 1, 858, 40, t, drop=4)
        + f'<rect x="2" y="2" width="{tag_width:.0f}" height="38" fill="{t["tag_bg"]}"/>'
        + pixel_text(tag, 17, 14, 2, t["tag_fg"], tracking=1.5, bold=True)
        + f'<text class="c" x="{tag_width + 14:.0f}" y="28" font-size="18" fill="{t["ink"]}">{title}</text>'
        + f'<rect x="{vent_x:.0f}" y="11" width="{816 - vent_x:.0f}" height="20" fill="url(#vent)" opacity=".85"/>'
        + f'<rect x="832" y="15" width="11" height="11" fill="{led}" stroke="{t["line"]}" stroke-width="1.2"/>'
    )
    return svg(860, 46, f"{title} · {tag}", body, t, vent_height=20)


STEPS = (
    ("01", "开始收集", "/distill add", MINT),
    ("02", "随时查看", "/distill status", SUN),
    ("03", "导出文本", "/distill export", CORAL),
    ("04", "酿成人设", "交给大模型归纳", MINT),
)


def flow(t: dict) -> str:
    line = t["line"]
    width, gap = 186, 36
    parts = []
    for index, (number, title, detail, color) in enumerate(STEPS):
        x = 8 + index * (width + gap)
        parts.append(bevel(x, 8, width, 120, t, drop=5))
        parts.append(f'<rect x="{x + 1}" y="9" width="{width - 2}" height="8" fill="{color}"/>')
        parts.append(f'<line x1="{x + 1}" y1="17" x2="{x + width - 1}" y2="17" stroke="{line}" stroke-width="1.5"/>')
        parts.append(pixel_text(number, x + 16, 32, 3.2, t["ink"], tracking=1, bold=True))
        parts.append(f'<rect x="{x + 16}" y="62" width="22" height="3" fill="{color}"/>')
        parts.append(f'<text class="c" x="{x + 16}" y="90" font-size="17" fill="{t["ink"]}">{title}</text>')
        mono = detail.startswith("/")
        family = ' font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"' if mono else ""
        parts.append(f'<text class="c"{family} x="{x + 16}" y="113" font-size="12.5" fill="{t["sub"]}">{detail}</text>')
        if index < len(STEPS) - 1:
            ax = x + width + 8
            arrow = ["1000", "1100", "1110", "1111", "1110", "1100", "1000"]
            d = "".join(
                f"M{ax + 4 + c * 3} {56 + r * 3}h3v3h-3z"
                for r, bits in enumerate(arrow) for c, bit in enumerate(bits) if bit == "1"
            )
            parts.append(f'<rect x="{ax - 4}" y="65" width="10" height="3" fill="{t["sub"]}"/>')
            parts.append(f'<path d="{d}" fill="{t["sub"]}"/>')
    return svg(860, 140, "工作流程：开始收集、随时查看、导出文本、酿成人设", "".join(parts), t)


def main() -> None:
    for mode, t in THEMES.items():
        (OUT / f"hero-{mode}.svg").write_text(hero(t), encoding="utf-8")
        (OUT / f"flow-{mode}.svg").write_text(flow(t), encoding="utf-8")
        for key in SECTIONS:
            (OUT / f"sec-{key}-{mode}.svg").write_text(section(key, t), encoding="utf-8")


if __name__ == "__main__":
    main()
