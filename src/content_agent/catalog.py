"""Validate externally supplied synthetic materials and fee/time tables."""


def validate_catalog(catalog):
    if not isinstance(catalog,dict) or not isinstance(catalog.get("version"),str) or not catalog["version"].strip():
        raise ValueError("catalog.version 须为非空字符串")
    common = catalog.get("common")
    variants = catalog.get("variants")
    if not isinstance(common,dict) or set(common) != {"search_assets","estimate_cost","save_plan"}:
        raise ValueError("catalog.common 须包含检索、估算、保存三类费用")
    if not isinstance(variants,dict) or set(variants) != {"studio","lite"}:
        raise ValueError("catalog.variants 须包含 studio 和 lite")
    for name, spec in [*common.items(),*variants.items()]:
        if not isinstance(spec,dict) or any(type(spec.get(k)) is not int or spec[k] < 0 for k in ("units","minutes")):
            raise ValueError(f"catalog.{name} 的 units、minutes 须为非负整数")
    for name, needs in (("studio",["clip","image"]),("lite",["any"])):
        spec = variants[name]
        if spec.get("requires") != needs or any(not isinstance(spec.get(k),str) or not spec[k].strip() for k in ("label","channel")):
            raise ValueError(f"catalog.{name} 的素材依赖或说明无效")
    assets = catalog.get("assets")
    if not isinstance(assets,list):
        raise ValueError("catalog.assets 须为数组")
    ids = set()
    for asset in assets:
        if not isinstance(asset,dict) or any(not isinstance(asset.get(k),str) or not asset[k].strip()
                                             for k in ("id","title","detail")):
            raise ValueError("素材须包含非空 id、title、detail")
        if asset["id"] in ids or asset.get("kind") not in ("clip","image"):
            raise ValueError("素材 ID 须唯一，kind 须为 clip 或 image")
        tags = asset.get("tags")
        if not isinstance(tags,list) or not tags or any(not isinstance(tag,str) or not tag.strip() for tag in tags):
            raise ValueError("素材 tags 须为非空字符串数组")
        ids.add(asset["id"])
