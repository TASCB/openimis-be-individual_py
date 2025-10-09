from individual.reports import eligible_households
from individual.reports import eligible_individuals
from individual.reports.households import eligible_households_query
from individual.reports.households import eligible_individuals_query

report_definitions = [
    {
        "name": "eligible_households_report",
        "engine": 0,
        "default_report": eligible_households.template,
        "description": "Eligible households report",
        "module": "individual",
        "python_query": eligible_households_query,
        "permission": ["131215"],
    },
    {
        "name": "eligible_individuals_report",
        "engine": 0,
        "default_report": eligible_individuals.template,
        "description": "Eligible individuals report",
        "module": "individual",
        "python_query": eligible_individuals_query,
        "permission": ["131215"],
    },
]