import os
import json
from core.models import ModuleConfiguration
from individual.tests.test_helpers import (
    IndividualGQLTestCase,
)


class IndividualCustomFilterQueryTest(IndividualGQLTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_config_path = os.path.join(
            os.path.dirname(__file__), 'fixtures', 'individual_config.json'
        )

    def test_individual_custom_filter_query(self):
        # First set the individual config to be empty
        config = ModuleConfiguration.objects.filter(module='individual', layer='be')
        if not config:
            config = ModuleConfiguration(module='individual', layer='be', config='{}')
        else:
            config.config = '{}'
        config.save()

        query_str = '''
            {
              customFilters(
                moduleName: "individual",
                objectTypeName: "Individual",
                additionalParams: "{\\"type\\":\\"INDIVIDUAL\\"}"
              ){
                type
                code
                possibleFilters {
                    field
                    filter
                    type
                }
              }
            }
        '''

        response = self.query(
            query_str,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"}
        )
        self.assertResponseNoErrors(response)
        content = json.loads(response.content)
        possible_filters = content['data']['customFilters']['possibleFilters']
        self.assertEqual(possible_filters, [])

        # Then update individual config to with the fixture config
        with open(self.test_config_path, 'rb') as test_file:
            config.config = test_file.read()
        config.save()

        response = self.query(
            query_str,
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"}
        )
        self.assertResponseNoErrors(response)
        content = json.loads(response.content)
        possible_filters = content['data']['customFilters']['possibleFilters']
        self.assertTrue(len(possible_filters), 3)
        expected_possible_filters = [
            {'field': 'poor', 'filter': ['exact'], 'type': 'boolean'},
            {'field': 'educated_level', 'filter': ['iexact', 'istartswith', 'icontains'], 'type': 'string'},
            {'field': 'number_of_children', 'filter': ['exact', 'lt', 'lte', 'gt', 'gte'], 'type': 'integer'},
        ]
        for f in expected_possible_filters:
            self.assertTrue(f in possible_filters, f'expected to find {f} in {possible_filters}')
