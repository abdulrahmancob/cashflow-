"""SF clinic string ↔ WebPT facility mapping (fail-closed for unknown)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FacilityMapping:
    sf_clinic: str
    webpt_facility_id: str | None
    webpt_facility_name: str | None
    status: str  # mapped | alias | out_of_scope | unmapped


# Exact WebPT export names / IDs known in jun_jul patients_export_273d.
WEBPT_FACILITIES: dict[str, str] = {
    "21533": "Sheepshead Bay",
    "21534": "Bay Ridge",
    "21535": "Inwood",
    "24151": "Corona",
    "24980": "Jamaica",
    "26445": "BensonHurst - Adults",
    "26553": "Bedstuy",
    "27480": "Flatbush",
    "27690": "Bushwick",
    "28029": "Brownsville",
    "28719": "Sugar Hill",
    "29806": "Huntspoint",
    "30210": "Castle Hill",
    "30701": "Riverdale",
    "30874": "Allerton",
    "30926": "Central Harlem",
    "30951": "Clinton Hill",
    "31195": "Fordham",
    "31320": "Midtown",
    "31370": "Midwood",
    "31584": "Upper East Side",
    "31674": "Church Ave",
    "32380": "Cobble Hill",
    "32415": "Tribeca",
    "32416": "Greenpoint",
    "32417": "Astoria",
    "32418": "Sunset - Adults",
    "33220": "Lenox hill",
    "33221": "Hell's Kitchen",
    "33643": "Chelsea",
    "34088": "Grand Concourse",
    "34312": "Belmont",
}

# SF CLINIC string → WebPT facility_id
SF_TO_WEBPT: dict[str, str] = {
    "Sheepshead Bay": "21533",
    "sheepshead": "21533",
    "Bay Ridge": "21534",
    "Inwood": "21535",
    "Corona": "24151",
    "Jamaica": "24980",
    "BensonHurst - Adults": "26445",
    "Bensonhurst": "26445",
    "Bedstuy": "26553",
    "Flatbush": "27480",
    "FlatBush": "27480",
    "Bushwick": "27690",
    "Brownsville": "28029",
    "Sugar Hill": "28719",
    "Huntspoint": "29806",
    "Castle Hill": "30210",
    "Riverdale": "30701",
    "Allerton": "30874",
    "Central Harlem": "30926",
    "Clinton Hill": "30951",
    "Fordham": "31195",
    "Midtown": "31320",
    "Midwood": "31370",
    "Upper East Side": "31584",
    "Church Ave": "31674",
    "Cobble Hill": "32380",
    "Tribeca": "32415",
    "Greenpoint": "32416",
    "Astoria": "32417",
    "Sunset - Adults": "32418",
    "Sunset": "32418",
    "Lenox hill": "33220",
    "Lenox Hill": "33220",
    "Hell's Kitchen": "33221",
    "Chelsea": "33643",
    "Grand Concourse": "34088",
    "Belmont": "34312",
    # Known SF-only / out of WebPT company export scope (until list_clinics proves otherwise)
    "Home Care": "",
    "Sensory Freeway PT": "",
    "Sensory Freeway OT": "",
}

OUT_OF_SCOPE = frozenset({"Home Care", "Sensory Freeway PT", "Sensory Freeway OT"})


def map_sf_clinic(sf_clinic: str) -> FacilityMapping:
    name = (sf_clinic or "").strip()
    if not name:
        return FacilityMapping(name, None, None, "unmapped")
    if name in OUT_OF_SCOPE:
        return FacilityMapping(name, None, None, "out_of_scope")
    fid = SF_TO_WEBPT.get(name)
    if fid is None:
        # case-insensitive fallback
        lower = {k.lower(): v for k, v in SF_TO_WEBPT.items()}
        fid = lower.get(name.lower())
    if fid is None:
        return FacilityMapping(name, None, None, "unmapped")
    if fid == "":
        return FacilityMapping(name, None, None, "out_of_scope")
    webpt_name = WEBPT_FACILITIES.get(fid)
    status = "alias" if webpt_name and webpt_name != name else "mapped"
    return FacilityMapping(name, fid, webpt_name, status)


def assert_scrape_allowed(sf_clinic: str) -> FacilityMapping:
    m = map_sf_clinic(sf_clinic)
    if m.status in {"unmapped", "out_of_scope"} or not m.webpt_facility_id:
        raise RuntimeError(
            f"Scrape blocked for clinic {sf_clinic!r}: status={m.status}. "
            "Map via list_clinics / P4 before rediscovery."
        )
    return m
