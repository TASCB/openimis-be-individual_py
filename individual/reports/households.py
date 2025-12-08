import logging
from individual.models import Individual
from django.db.models import Q, F
from location.models import Location
from core.models import User

# def insuree_family_overview_query(user, date_from=None, date_to=None, **kwargs):
#     filters = Q()
#
#     if date_from:
#         filters &= Q(validity_from__gte=date_from)
#
#     queryset = (
#         Insuree.objects.filter(filters).values(
#             "chf_id",
#             "other_names",
#             "last_name",
#             enroll_date=F("validity_from")
#         )
#     )
#
#     return {
#         "data": list(queryset)
#     }


def eligible_households_query(user, **kwargs):
    filters = Q()

    region = None
    district = None
    ward = None
    village = None

    # if 'pmt_class' in kwargs:
    #     filters &= Q(pmt_class__gte=kwargs.get('pmt_class'))
    if 'region_id' in kwargs:
        region = Location.objects.filter(
            type="R",
            id=kwargs.get('region_id'),
        ).first()
    if 'district_id' in kwargs:
        district = Location.objects.filter(
            type="D",
            id=kwargs.get('district_id'),
            parent_id=region.id if region else None
        ).first()
    if 'ward_id' in kwargs:
        ward = Location.objects.filter(
            type="W",
            id=kwargs.get('ward_id'),
            parent_id=district.id if district else None
        ).first()
    if 'village_id' in kwargs:
        village = Location.objects.filter(
            type="V",
            id=kwargs.get('village_id'),
            parent_id=ward.id if ward else None
        ).first()

    locations = Location.objects.filter(type="R")

    households = User.objects.filter(filters)

    return {
        "region_name": region.name if region else "Unknown",
        "district_name": district.name if district else "Unknown",
        "ward_name": ward.name if ward else "Unknown",
        "village_name": getattr(village, 'name', 'Unknown'),
        "households": [
            {
                "id": str(getattr(user, "id", "")),
                "head": household.other_names,
                "number": household.username,
                "representative": household.last_name,
            } for household in (households or [])
        ]
    }


def eligible_individuals_query(user, **kwargs):
    filters = Q()

    if 'pmt_class' in kwargs:
        filters &= Q(pmt_class__gte=kwargs.get('pmt_class'))
    if 'region_id' in kwargs:
        filters &= Q(region_id=kwargs.get('region_id'))

    queryset = Individual.filter(filters)

    return {
        "users": []
    }