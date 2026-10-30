# -*- coding: utf-8 -*-
"""VoiceStudio 的 After Effects 渲染引擎。

对外只露三个东西：`STYLES` / `render()` / `public_styles()`，
其余细节都在 `ae_build.py` 里，模板都 在 `jsx/` 下。
"""
from .ae_build import STYLES, render, public_styles, style_by_key, find_ae  # noqa: F401

__all__ = ["STYLES", "render", "public_styles", "style_by_key", "find_ae"]
