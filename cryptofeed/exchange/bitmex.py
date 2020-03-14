'''
Copyright (C) 2017-2020  Bryant Moscon - bmoscon@gmail.com

Please see the LICENSE file for the terms and conditions
associated with this software.
'''
import json
import logging
from collections import defaultdict
from decimal import Decimal

import requests
from sortedcontainers import SortedDict as sd

from cryptofeed.feed import Feed
from cryptofeed.defines import L2_BOOK, BUY, SELL, BID, ASK, TRADES, FUNDING, BITMEX, OPEN_INTEREST, TICKER
from cryptofeed.standards import timestamp_normalize


LOG = logging.getLogger('feedhandler')


class Bitmex(Feed):
    id = BITMEX
    api = 'https://www.bitmex.com/api/v1/'

    def __init__(self, pairs=None, channels=None, callbacks=None, **kwargs):
        super().__init__('wss://www.bitmex.com/realtime', pairs=pairs, channels=channels, callbacks=callbacks, **kwargs)

        active_pairs = self.get_active_symbols()
        if self.config:
            pairs = list(self.config.values())
            self.pairs = [pair for inner in pairs for pair in inner]

        for pair in self.pairs:
            if not pair.startswith('.'):
                if pair not in active_pairs:
                    raise ValueError("{} is not active on BitMEX".format(pair))
        self._reset()

    def _reset(self):
        self.partial_received = defaultdict(bool)
        self.order_id = {}
        for pair in self.pairs:
            self.l2_book[pair] = {BID: sd(), ASK: sd()}
            self.order_id[pair] = defaultdict(dict)

    @staticmethod
    def get_symbol_info():
        return requests.get(Bitmex.api + 'instrument/').json()

    @staticmethod
    def get_active_symbols_info():
        return requests.get(Bitmex.api + 'instrument/active').json()

    @staticmethod
    def get_active_symbols():
        symbols = []
        for data in Bitmex.get_active_symbols_info():
            symbols.append(data['symbol'])
        return symbols

    async def _trade(self, msg: dict, timestamp: float):
        """
        trade msg example

        {
            'timestamp': '2018-05-19T12:25:26.632Z',
            'symbol': 'XBTUSD',
            'side': 'Buy',
            'size': 40,
            'price': 8335,
            'tickDirection': 'PlusTick',
            'trdMatchID': '5f4ecd49-f87f-41c0-06e3-4a9405b9cdde',
            'grossValue': 479920,
            'homeNotional': Decimal('0.0047992'),
            'foreignNotional': 40
        }
        """
        for data in msg['data']:
            ts = timestamp_normalize(self.id, data['timestamp'])
            await self.callback(TRADES, feed=self.id,
                                         pair=data['symbol'],
                                         side=BUY if data['side'] == 'Buy' else SELL,
                                         amount=Decimal(data['size']),
                                         price=Decimal(data['price']),
                                         order_id=data['trdMatchID'],
                                         timestamp=ts,
                                         receipt_timestamp=timestamp)

    async def _book(self, msg: dict, timestamp: float):
        """
        the Full bitmex book
        """
        delta = {BID: [], ASK: []}
        # if we reset the book, force a full update
        forced = False
        pair = msg['data'][0]['symbol']
        if not self.partial_received[pair]:
            # per bitmex documentation messages received before partial
            # should be discarded
            if msg['action'] != 'partial':
                return
            self.partial_received[pair] = True
            forced = True

        if msg['action'] == 'partial':
            for data in msg['data']:
                side = BID if data['side'] == 'Buy' else ASK
                price = Decimal(data['price'])
                size = Decimal(data['size'])
                order_id = data['id']

                self.l2_book[pair][side][price] = size
                self.order_id[pair][side][order_id] = price
        elif msg['action'] == 'insert':
            for data in msg['data']:
                side = BID if data['side'] == 'Buy' else ASK
                price = Decimal(data['price'])
                size = Decimal(data['size'])
                order_id = data['id']

                self.l2_book[pair][side][price] = size
                self.order_id[pair][side][order_id] = price
                delta[side].append((price, size))
        elif msg['action'] == 'update':
            for data in msg['data']:
                side = BID if data['side'] == 'Buy' else ASK
                update_size = Decimal(data['size'])
                order_id = data['id']

                price = self.order_id[pair][side][order_id]

                self.l2_book[pair][side][price] = update_size
                self.order_id[pair][side][order_id] = price
                delta[side].append((price, update_size))
        elif msg['action'] == 'delete':
            for data in msg['data']:
                side = BID if data['side'] == 'Buy' else ASK
                order_id = data['id']

                delete_price = self.order_id[pair][side][order_id]
                del self.order_id[pair][side][order_id]
                del self.l2_book[pair][side][delete_price]
                delta[side].append((delete_price, 0))

        else:
            LOG.warning("%s: Unexpected l2 Book message %s", self.id, msg)
            return

        await self.book_callback(self.l2_book[pair], L2_BOOK, pair, forced, delta, timestamp, timestamp)

    async def _ticker(self, msg: dict, timestamp: float):
        for data in msg['data']:
            await self.callback(TICKER, feed=self.id,
                            pair=data['symbol'],
                            bid=Decimal(data['bidPrice']),
                            ask=Decimal(data['askPrice']),
                            timestamp=timestamp_normalize(self.id, data['timestamp']),
                            receipt_timestamp=timestamp)

    async def _funding(self, msg: dict, timestamp: float):
        """
        {'table': 'funding',
         'action': 'partial',
         'keys': ['timestamp', 'symbol'],
         'types': {
             'timestamp': 'timestamp',
             'symbol': 'symbol',
             'fundingInterval': 'timespan',
             'fundingRate': 'float',
             'fundingRateDaily': 'float'
            },
         'foreignKeys': {
             'symbol': 'instrument'
            },
         'attributes': {
             'timestamp': 'sorted',
             'symbol': 'grouped'
            },
         'filter': {'symbol': 'XBTUSD'},
         'data': [{
             'timestamp': '2018-08-21T20:00:00.000Z',
             'symbol': 'XBTUSD',
             'fundingInterval': '2000-01-01T08:00:00.000Z',
             'fundingRate': Decimal('-0.000561'),
             'fundingRateDaily': Decimal('-0.001683')
            }]
        }
        """
        for data in msg['data']:
            ts = timestamp_normalize(self.id, data['timestamp'])
            await self.callback(FUNDING, feed=self.id,
                                          pair=data['symbol'],
                                          timestamp=ts,
                                          receipt_timestamp=timestamp,
                                          interval=data['fundingInterval'],
                                          rate=data['fundingRate'],
                                          rate_daily=data['fundingRateDaily']
                                          )

    async def _instrument(self, msg: dict, timestamp: float):
        """
        Example instrument data

        {
        'table':'instrument',
        'action':'partial',
        'keys':[
            'symbol'
        ],
        'types':{
            'symbol':'symbol',
            'rootSymbol':'symbol',
            'state':'symbol',
            'typ':'symbol',
            'listing':'timestamp',
            'front':'timestamp',
            'expiry':'timestamp',
            'settle':'timestamp',
            'relistInterval':'timespan',
            'inverseLeg':'symbol',
            'sellLeg':'symbol',
            'buyLeg':'symbol',
            'optionStrikePcnt':'float',
            'optionStrikeRound':'float',
            'optionStrikePrice':'float',
            'optionMultiplier':'float',
            'positionCurrency':'symbol',
            'underlying':'symbol',
            'quoteCurrency':'symbol',
            'underlyingSymbol':'symbol',
            'reference':'symbol',
            'referenceSymbol':'symbol',
            'calcInterval':'timespan',
            'publishInterval':'timespan',
            'publishTime':'timespan',
            'maxOrderQty':'long',
            'maxPrice':'float',
            'lotSize':'long',
            'tickSize':'float',
            'multiplier':'long',
            'settlCurrency':'symbol',
            'underlyingToPositionMultiplier':'long',
            'underlyingToSettleMultiplier':'long',
            'quoteToSettleMultiplier':'long',
            'isQuanto':'boolean',
            'isInverse':'boolean',
            'initMargin':'float',
            'maintMargin':'float',
            'riskLimit':'long',
            'riskStep':'long',
            'limit':'float',
            'capped':'boolean',
            'taxed':'boolean',
            'deleverage':'boolean',
            'makerFee':'float',
            'takerFee':'float',
            'settlementFee':'float',
            'insuranceFee':'float',
            'fundingBaseSymbol':'symbol',
            'fundingQuoteSymbol':'symbol',
            'fundingPremiumSymbol':'symbol',
            'fundingTimestamp':'timestamp',
            'fundingInterval':'timespan',
            'fundingRate':'float',
            'indicativeFundingRate':'float',
            'rebalanceTimestamp':'timestamp',
            'rebalanceInterval':'timespan',
            'openingTimestamp':'timestamp',
            'closingTimestamp':'timestamp',
            'sessionInterval':'timespan',
            'prevClosePrice':'float',
            'limitDownPrice':'float',
            'limitUpPrice':'float',
            'bankruptLimitDownPrice':'float',
            'bankruptLimitUpPrice':'float',
            'prevTotalVolume':'long',
            'totalVolume':'long',
            'volume':'long',
            'volume24h':'long',
            'prevTotalTurnover':'long',
            'totalTurnover':'long',
            'turnover':'long',
            'turnover24h':'long',
            'homeNotional24h':'float',
            'foreignNotional24h':'float',
            'prevPrice24h':'float',
            'vwap':'float',
            'highPrice':'float',
            'lowPrice':'float',
            'lastPrice':'float',
            'lastPriceProtected':'float',
            'lastTickDirection':'symbol',
            'lastChangePcnt':'float',
            'bidPrice':'float',
            'midPrice':'float',
            'askPrice':'float',
            'impactBidPrice':'float',
            'impactMidPrice':'float',
            'impactAskPrice':'float',
            'hasLiquidity':'boolean',
            'openInterest':'long',
            'openValue':'long',
            'fairMethod':'symbol',
            'fairBasisRate':'float',
            'fairBasis':'float',
            'fairPrice':'float',
            'markMethod':'symbol',
            'markPrice':'float',
            'indicativeTaxRate':'float',
            'indicativeSettlePrice':'float',
            'optionUnderlyingPrice':'float',
            'settledPrice':'float',
            'timestamp':'timestamp'
        },
        'foreignKeys':{
            'inverseLeg':'instrument',
            'sellLeg':'instrument',
            'buyLeg':'instrument'
        },
        'attributes':{
            'symbol':'unique'
        },
        'filter':{
            'symbol':'XBTUSD'
        },
        'data':[
            {
                'symbol':'XBTUSD',
                'rootSymbol':'XBT',
                'state':'Open',
                'typ':'FFWCSX',
                'listing':'2016-05-13T12:00:00.000Z',
                'front':'2016-05-13T12:00:00.000Z',
                'expiry':None,
                'settle':None,
                'relistInterval':None,
                'inverseLeg':'',
                'sellLeg':'',
                'buyLeg':'',
                'optionStrikePcnt':None,
                'optionStrikeRound':None,
                'optionStrikePrice':None,
                'optionMultiplier':None,
                'positionCurrency':'USD',
                'underlying':'XBT',
                'quoteCurrency':'USD',
                'underlyingSymbol':'XBT=',
                'reference':'BMEX',
                'referenceSymbol':'.BXBT',
                'calcInterval':None,
                'publishInterval':None,
                'publishTime':None,
                'maxOrderQty':10000000,
                'maxPrice':1000000,
                'lotSize':1,
                'tickSize':Decimal(         '0.5'         ),
                'multiplier':-100000000,
                'settlCurrency':'XBt',
                'underlyingToPositionMultiplier':None,
                'underlyingToSettleMultiplier':-100000000,
                'quoteToSettleMultiplier':None,
                'isQuanto':False,
                'isInverse':True,
                'initMargin':Decimal(         '0.01'         ),
                'maintMargin':Decimal(         '0.005'         ),
                'riskLimit':20000000000,
                'riskStep':10000000000,
                'limit':None,
                'capped':False,
                'taxed':True,
                'deleverage':True,
                'makerFee':Decimal(         '-0.00025'         ),
                'takerFee':Decimal(         '0.00075'         ),
                'settlementFee':0,
                'insuranceFee':0,
                'fundingBaseSymbol':'.XBTBON8H',
                'fundingQuoteSymbol':'.USDBON8H',
                'fundingPremiumSymbol':'.XBTUSDPI8H',
                'fundingTimestamp':'2020-02-02T04:00:00.000Z',
                'fundingInterval':'2000-01-01T08:00:00.000Z',
                'fundingRate':Decimal(         '0.000106'         ),
                'indicativeFundingRate':Decimal(         '0.0001'         ),
                'rebalanceTimestamp':None,
                'rebalanceInterval':None,
                'openingTimestamp':'2020-02-02T00:00:00.000Z',
                'closingTimestamp':'2020-02-02T01:00:00.000Z',
                'sessionInterval':'2000-01-01T01:00:00.000Z',
                'prevClosePrice':Decimal(         '9340.63'         ),
                'limitDownPrice':None,
                'limitUpPrice':None,
                'bankruptLimitDownPrice':None,
                'bankruptLimitUpPrice':None,
                'prevTotalVolume':1999389257669,
                'totalVolume':1999420432348,
                'volume':31174679,
                'volume24h':1605909209,
                'prevTotalTurnover':27967114248663460,
                'totalTurnover':27967447182062520,
                'turnover':332933399058,
                'turnover24h':17126993087717,
                'homeNotional24h':Decimal(         '171269.9308771703'         ),
                'foreignNotional24h':1605909209,
                'prevPrice24h':9348,
                'vwap':Decimal(         '9377.3443'         ),
                'highPrice':9464,
                'lowPrice':Decimal(         '9287.5'         ),
                'lastPrice':9352,
                'lastPriceProtected':9352,
                'lastTickDirection':'ZeroMinusTick',
                'lastChangePcnt':Decimal(         '0.0004'         ),
                'bidPrice':9352,
                'midPrice':Decimal(         '9352.25'         ),
                'askPrice':Decimal(         '9352.5'         ),
                'impactBidPrice':Decimal(         '9351.9125'         ),
                'impactMidPrice':Decimal(         '9352.25'         ),
                'impactAskPrice':Decimal(         '9352.7871'         ),
                'hasLiquidity':True,
                'openInterest':983043322,
                'openValue':10518563545400,
                'fairMethod':'FundingRate',
                'fairBasisRate':Decimal(         '0.11607'         ),
                'fairBasis':Decimal(         '0.43'         ),
                'fairPrice':Decimal(         '9345.36'         ),
                'markMethod':'FairPrice',
                'markPrice':Decimal(         '9345.36'         ),
                'indicativeTaxRate':0,
                'indicativeSettlePrice':Decimal(         '9344.93'         ),
                'optionUnderlyingPrice':None,
                'settledPrice':None,
                'timestamp':'2020-02-02T00:30:43.772Z'
            }
        ]
        }
        """
        for data in msg['data']:
            if 'openInterest' in data:
                ts = timestamp_normalize(self.id, data['timestamp'])
                await self.callback(OPEN_INTEREST, feed=self.id,
                                                pair=data['symbol'],
                                                open_interest=data['openInterest'],
                                                timestamp=ts,
                                                receipt_timestamp=timestamp)

    async def message_handler(self, msg: str, timestamp: float):
        msg = json.loads(msg, parse_float=Decimal)
        if 'info' in msg:
            LOG.info("%s - info message: %s", self.id, msg)
        elif 'subscribe' in msg:
            if not msg['success']:
                LOG.error("%s: subscribe failed: %s", self.id, msg)
        elif 'error' in msg:
            LOG.error("%s: Error message from exchange: %s", self.id, msg)
        else:
            if msg['table'] == 'trade':
                await self._trade(msg, timestamp)
            elif msg['table'] == 'orderBookL2':
                await self._book(msg, timestamp)
            elif msg['table'] == 'funding':
                await self._funding(msg, timestamp)
            elif msg['table'] == 'instrument':
                await self._instrument(msg, timestamp)
            elif msg['table'] == 'quote':
                await self._ticker(msg, timestamp)
            else:
                LOG.warning("%s: Unhandled message %s", self.id, msg)

    async def subscribe(self, websocket):
        self._reset()
        chans = []
        for channel in self.channels if not self.config else self.config:
            for pair in self.pairs if not self.config else self.config[channel]:
                chans.append("{}:{}".format(channel, pair))

        await websocket.send(json.dumps({"op": "subscribe",
                                         "args": chans}))
