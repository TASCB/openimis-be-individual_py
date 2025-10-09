from individual.models import Individual


def eligible_households_query(user, type, region_id=None, **kwargs):
    individuals = Individual.objects.all()
    return { "data": individuals }

def eligible_individuals_query(user, type, region_id=None, **kwargs):
    individuals = Individual.objects.all()
    return { "data": individuals }