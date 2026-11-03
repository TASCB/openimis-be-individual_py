# openIMIS Backend individual reference module
This repository holds the files of the openIMIS Backend Individual reference module.
It is dedicated to be deployed as a module of [openimis-be_py](https://github.com/openimis/openimis-be_py).

## ORM mapping:
* individual_individual, individual_historicalindividual > Individual
* individual_individualdatasource, individual_historicalindividualdatasource > IndividualDataSource
* individual_individualdatasourceupload, individual_historicalindividualdatasourceupload > IndividualDataSourceUpload
* individual_group, individual_historicalgroup > Group
* individual_groupindividual, individual_historicalgroupindividual > GroupIndividual

## GraphQl Queries
* individual
* individualDataSource
* individualDataSourceUpload
* group
* groupIndividual
* groupExport
* individualExport
* groupIndividualExport

## GraphQL Mutations - each mutation emits default signals and return standard error lists (cfr. openimis-be-core_py)
* createIndividual
* updateIndividual
* deleteIndividual
* createGroup
* updateGroup
* deleteGroup
* addIndividualToGroup
* editIndividualInGroup
* removeIndividualFromGroup
* createGroupIndividuals

## Services
- Individual
  - create
  - update
  - delete
- IndividualDataSource
  - create
  - update
  - delete
- Group
  - create
  - update
  - delete
  - create_group_individuals
  - update_group_individuals
- GroupIndividualService
  - create
  - update
  - delete

## Configuration options (can be changed via core.ModuleConfiguration)
* gql_individual_search_perms: required rights to call individual GraphQL Query (default: ["159001"])
* gql_individual_create_perms: required rights to call createIndividual GraphQL Mutation (default: ["159002"])
* gql_individual_update_perms: required rights to call updateIndividual GraphQL Mutation (default: ["159003"])
* gql_individual_delete_perms: required rights to call deleteIndividual GraphQL Mutation (default: ["159004"])
* gql_group_search_perms: required rights to call group GraphQL Mutation (default: ["180001"])
* gql_group_create_perms: required rights to call createGroup and addIndividualToGroup and createGroupIndividuals GraphQL Mutation (default: ["180002"])
* gql_group_update_perms: required rights to call updateGroup and editIndividualInGroup GraphQL Mutation (default: ["180003"])
* gql_group_delete_perms: required rights to call deleteGroup and removeIndividualFromGroup GraphQL Mutation (default: ["180004"])


## openIMIS Modules Dependencies
- core


## Enabling Python Workflows
Module comes with simple workflows for individual data upload. 
They should be used for the development purposes, not in production environment. 
To activate these Python workflows, a configuration change is required. 
Specifically, the `enable_python_workflows` parameter to `true` within module config.

Workflows: 
 * individual upload


## Additional Field Definition

Individual model comes with a minimal set of fields: `first_name`, `last_name`, `dob`.
To add additional fields, define them in the backend admin interface by adding a Module configuration for `individual`:

1. In the web app, visit URL path `/api/admin/core/moduleconfiguration` in browser
2. Click on ADD MODULE CONFIGURATION
3. Fill in the form with the following values:
    - Module: individual
    - Layer: backend
    - Version: 1
    - Config: `{"individual_schema": "{\"$id\": \"https://example.com/beneficiares.schema.json\", \"type\": \"object\", \"title\": \"Record of beneficiares\", \"$schema\": \"http://json-schema.org/draft-04/schema#\", \"properties\": {\"email\": {\"type\": \"string\", \"description\": \"email address to contact with beneficiary\", \"validationCalculation\": {\"name\": \"EmailValidationStrategy\"}}, \"able_bodied\": {\"type\": \"boolean\", \"description\": \"Flag determining whether someone is able bodied or not\"}, \"national_id\": {\"type\": \"string\", \"description\": \"national id\"}, \"educated_level\": {\"type\": \"string\", \"description\": \"The level of person when it comes to the school/education/studies\"}, \"chronic_illness\": {\"type\": \"boolean\", \"description\": \"Flag determining whether someone has such kind of illness or not\"}, \"national_id_type\": {\"type\": \"string\", \"description\": \"A type of national id\"}, \"number_of_elderly\": {\"type\": \"integer\", \"description\": \"Number of elderly\"}, \"number_of_children\": {\"type\": \"integer\", \"description\": \"Number of children\"}, \"beneficiary_data_source\": {\"type\": \"string\", \"description\": \"The source from where such beneficiary comes\"}}, \"description\": \"This document records the details beneficiares\"}"}`
   Modify the `Config` value accordingly with your individual additional field definitions.
4. Click on SAVE

### Generate dummy individuals for development

**Requirements**

- [openimis-be_py](https://github.com/openimis/openimis-be_py) repo is cloned and setup locally. Django command execution requires this repo.
- Ensure all dependencies are installed, see openimis-be_py [README](https://github.com/openimis/openimis-be_py/blob/develop/README.md#developers-setup)

First, generate a csv file with a list of individuals which can be used for uploading:

```bash
# go to the openimis-be_py repo, openIMIS folder
cd ../openimis-be_py/openIMIS/

python manage.py fake_individuals
```

The `fake_individuals` command generates 100 individuals using the sample `individual_schema` provided above.
Feel free to modify the fields and number of individuals as needed.
The last line of the output should provide the path to the temporary csv file that contains the list of dummy individuals.

Then upload the generated csv file in the web app:
- Go to "Social Protection" > "Individuals" > "UPLOAD" and select the generated csv as the file to upload.
- Choose "Python Import Individuals" as the Workflow, leave "Create groups from column:" empty, then click on UPLOAD INDIVIDUALS.
- Go to "Task Management" > "All Tasks", find and approve the `import_valid_items` task.
- Then you should see the list of individuals appear under "Social Protection" > "Individuals"

## Survey Solutions import optimization

Deploy with the matching ETL branch. See the
[optimization and database deployment README](https://github.com/TASCB/openimis-be-api_etl_py/blob/feature/survey-solutions-import-optimization/docs/performance/README.md)
for required migrations/indexes, deployment steps, and measured improvements.
