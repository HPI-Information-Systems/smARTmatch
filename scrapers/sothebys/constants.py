from __future__ import annotations

import re

BASE_CALENDAR_URL = (
    "https://www.sothebys.com/en/calendar?s=0&from=&to="
    "&f4=00000164-609b-d1db-a5e6-e9ff08ab0000"
    "&f4=00000164-609a-d1db-a5e6-e9fff79f0000"
    "&f4=00000164-609a-d1db-a5e6-e9fffe5f0000"
    "&f4=00000164-609b-d1db-a5e6-e9ff01850000"
    "&f4=00000164-609b-d1db-a5e6-e9ff0a800000"
    "&f4=00000164-609a-d1db-a5e6-e9fff35f0000"
    "&f4=00000164-609a-d1db-a5e6-e9fff8660000"
    "&f4=00000164-609a-d1db-a5e6-e9fffa760000"
    "&f4=00000164-609b-d1db-a5e6-e9ff07220000"
    "&f4=00000164-609b-d1db-a5e6-e9ff09100000"
    "&f4=00000164-609b-d1db-a5e6-e9ff0a350000"
    "&f4=00000164-609b-d1db-a5e6-e9ff07e20000"
    "&f4=00000164-609a-d1db-a5e6-e9fffadc0000"
    "&f4=00000164-609a-d1db-a5e6-e9fffec40000"
    "&f4=00000164-609a-d1db-a5e6-e9fffec40000"
    "&q="
)

SOTHEBYS_ORIGIN = "https://www.sothebys.com"

HREF_PATTERN = re.compile(r'href\s*=\s*"([^"]+)"')
NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

GRAPHQL_ENDPOINT = "https://clientapi.prod.sothelabs.com/graphql"

LOT_CARDS_QUERY = """
query LotCardsFilterByPaginated($id: String!, $filter: LotCardsConnectionFilter!, $language: TranslationLanguage!, $limit: Int, $offset: Int) {
  auction(id: $id, language: $language) {
    id
    lotCards: lotCardsConnection(offset: $offset, limit: $limit, filter: $filter) {
      lots { lotId }
      hasNextPage
      totalCount
    }
  }
}
"""

GRAPHQL_QUERY = """
query LotQuery($id: String!, $countryOfOrigin: String, $language: TranslationLanguage!) {
  lot: lotV2(lotId: $id, countryOfOrigin: $countryOfOrigin, language: $language) {
    __typename
    ... on LotV2 {
      lotId
      title
      creatorsDisplayTitle
      description
      catalogueNote
      provenance
      exhibition
      literature
      estimateV2 {
        __typename
        ... on LowHighEstimateV2 {
          lowEstimate { __typename amount }
          highEstimate { __typename amount }
        }
      }
      lotNumber {
        __typename
        ... on VisibleLotNumber {
          lotNumber
        }
      }
      slug
      auction {
        auctionId
        title
        location
        departmentNames
        dates {
          __typename
          acceptsBids
          ... on LiveAuctionDates {
            goesLive
            published
            closed
          }
        }
        slug {
          year
          name
        }
      }
      media(imageSizes: [Small, Medium, Large, ExtraLarge, ExtraExtraLarge]) {
        images {
          title
          renditions {
            width
            height
            url
            imageSize
          }
        }
      }
    }
    ... on HiddenLot {
      lotId
      auction {
        auctionId
        title
        slug {
          year
          name
        }
      }
    }
  }
}
"""
