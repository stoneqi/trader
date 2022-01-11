# coding=utf-8
#
# Copyright 2016 timercrack
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import asyncio
import random
import re
from collections import defaultdict
import datetime
import pytz
from decimal import Decimal

from django.db.models import Q, Max, Min, Sum
from talib import ATR
import ujson as json
import aioredis

from trader.strategy import BaseModule
from trader.utils.func_container import param_function
from trader.utils.read_config import *
from trader.utils.my_logger import get_my_logger
from trader.utils import ApiStruct, price_round, is_trading_day, update_from_shfe, update_from_dce, \
    update_from_czce, update_from_cffex, get_contracts_argument, calc_main_inst, str_to_number
from panel.models import *

logger = get_my_logger('CTPApi')
HANDLER_TIME_OUT = config.getint('TRADE', 'command_timeout', fallback=10)


class TradeStrategy(BaseModule):
    def __init__(self, name: str, io_loop: asyncio.AbstractEventLoop = None):
        super().__init__(io_loop)
        self.__market_response_format = config.get('MSG_CHANNEL', 'market_response_format')
        self.__trade_response_format = config.get('MSG_CHANNEL', 'trade_response_format')
        self.__request_format = config.get('MSG_CHANNEL', 'request_format')
        self.__ignore_inst_list = config.get('TRADE', 'ignore_inst').split(',')
        self.__request_id = random.randint(0, 65535)
        self.__order_ref = random.randint(0, 999)
        self.__strategy = Strategy.objects.get(name=name)
        self.__inst_ids = self.__strategy.instruments.all().values_list('product_code', flat=True)
        self.__broker = self.__strategy.broker
        self.__fake = self.__broker.fake  # 虚拟资金
        self.__current = self.__broker.current  # 当前动态权益
        self.__pre_balance = self.__broker.pre_balance  # 静态权益
        self.__cash = self.__broker.cash  # 可用资金
        self.__shares = dict()  # { instrument : position }
        self.__cur_account = None
        self.__margin = 0  # 占用保证金
        self.__activeOrders = {}  # 未成交委托单
        self.__re_extract_code = re.compile(r'([a-zA-Z]*)(\d+)')  # 提合约字母部分 IF1509 -> IF
        self.__re_extract_name = re.compile('(.*?)([0-9]+)(.*?)$')  # 提取合约文字部分

    def update_account(self, account: dict):
        # 静态权益=上日结算-出金金额+入金金额
        self.__pre_balance = Decimal(account['PreBalance']) - Decimal(account['Withdraw']) + \
                             Decimal(account['Deposit'])
        # 动态权益=静态权益+平仓盈亏+持仓盈亏-手续费
        self.__current = self.__pre_balance + Decimal(account['CloseProfit']) + \
            Decimal(account['PositionProfit']) - Decimal(account['Commission'])
        self.__margin = Decimal(account['CurrMargin'])
        self.__cash = Decimal(account['Available'])
        self.__cur_account = account
        self.__broker = Broker.objects.get(username=account['AccountID'])
        self.__fake = self.__broker.fake
        self.__broker.cash = self.__cash
        self.__broker.current = self.__current
        self.__broker.pre_balance = self.__pre_balance
        self.__broker.save(update_fields=['cash', 'current', 'pre_balance'])
        logger.info("可用资金: {:,.0f} 静态权益: {:,.0f} 动态权益: {:,.0f} 虚拟: {:,.0f}".format(
            self.__cash, self.__pre_balance, self.__current, self.__fake))
        self.__strategy = self.__broker.strategy_set.first()
        self.__inst_ids = [inst.product_code for inst in self.__strategy.instruments.all()]

    def update_position(self):
        for _, pos in self.__shares.items():
            try:
                p_code = re.findall('[A-Za-z]+', pos['InstrumentID'])[0]
                inst = Instrument.objects.filter(
                    exchange=pos['ExchangeID'],
                    product_code=p_code).first()
                if not inst:
                    logger.error(f"update_position 未发现合约 {pos['InstrumentID']}")
                    continue
                bar = DailyBar.objects.filter(
                    exchange=inst.exchange, code=pos['InstrumentID']).order_by('-time').first()
                if not bar:
                    logger.error(f"update_position 未发现日线数据 {pos['InstrumentID']}")
                    continue
                if pos['Direction'] == ApiStruct.D_Buy:
                    profit = Decimal(bar.settlement) - Decimal(pos['OpenPrice'])
                else:
                    profit = Decimal(pos['OpenPrice']) - Decimal(bar.settlement)
                profit = profit * Decimal(pos['Volume']) * inst.volume_multiple
                Trade.objects.update_or_create(
                    broker=self.__broker, strategy=self.__strategy, instrument=inst,
                    code=pos['InstrumentID'],
                    direction=DirectionType.LONG if pos['Direction'] == ApiStruct.D_Buy else DirectionType.SHORT,
                    close_time__isnull=True,
                    defaults={
                        'open_time': datetime.datetime.strptime(
                            pos['OpenDate']+'09', '%Y%m%d%H').replace(tzinfo=pytz.FixedOffset(480)),
                        'shares': pos['Volume'], 'filled_shares': pos['Volume'],
                        'avg_entry_price': Decimal(pos['OpenPrice']),
                        'cost': pos['Volume'] * Decimal(pos['OpenPrice']) * inst.fee_money *
                        inst.volume_multiple + pos['Volume'] * inst.fee_volume,
                        'profit': profit, 'frozen_margin': Decimal(pos['Margin'])})
            except Exception as ee:
                logger.info('update_position 发生错误: %s', repr(ee), exc_info=True)
                continue

    async def start(self):
        self.raw_redis.set('HEARTBEAT:TRADER', 1, ex=61)
        await self.query('TradingAccount')
        self.__shares.clear()
        await self.query('InvestorPositionDetail')
        # await self.processing_signal3()
        # await self.refresh_instrument()
        # await self.collect_tick_stop()
        # await self.collect_quote()
        # day = datetime.datetime.strptime('20161031', '%Y%m%d').replace(tzinfo=pytz.FixedOffset(480))
        # for inst in self.__strategy.instruments.all():
        #     # self.calc_signal(inst, day)
        #     self.process_signal(inst)
        order_list = await self.query('Order')
        for order in order_list:
            # 未成交订单
            if int(order['OrderStatus']) in range(1, 5) and \
                            order['OrderSubmitStatus'] == ApiStruct.OSS_Accepted:
                self.__activeOrders[order['OrderRef']] = order
                direct_str = '多' if order['Direction'] == ApiStruct.D_Buy else '空'
                logger.info(f"撤销未成交订单: 合约{order['InstrumentID']} {direct_str}单 {order['VolumeTotal']}手 "
                            f"价格{order['LimitPrice']}")
                await self.cancel_order(order)
        # await self.force_close_all()
        # 获取持仓合约的tick数据

    async def stop(self):
        pass

    def next_order_ref(self):
        self.__order_ref = 1 if self.__order_ref == 999 else self.__order_ref + 1
        now = datetime.datetime.now()
        return '{:02}{:02}{:02}{:03}{:03}'.format(
            now.hour, now.minute, now.second, int(now.microsecond / 1000), self.__order_ref)

    def next_id(self):
        self.__request_id = 1 if self.__request_id == 65535 else self.__request_id + 1
        return self.__request_id

    def getShares(self, instrument: str):
        # 这个函数只能处理持有单一方向仓位的情况，若同时持有多空的头寸，返回结果不正确
        shares = 0
        pos_price = 0
        for pos in self.__shares[instrument]:
            pos_price += pos['Volume'] * pos['OpenPrice']
            shares += pos['Volume'] * (-1 if pos['Direction'] == ApiStruct.D_Sell else 1)
        return shares, pos_price / abs(shares), self.__shares[instrument][0]['OpenDate']

    def getPositions(self, inst_id: int):
        # 这个函数只能处理持有单一方向仓位的情况，若同时持有多空的头寸，返回结果不正确
        return self.__shares[inst_id][0]

    def async_query(self, query_type: str, **kwargs):
        request_id = self.next_id()
        kwargs['RequestID'] = request_id
        self.raw_redis.publish(self.__request_format.format('ReqQry' + query_type), json.dumps(kwargs))

    @staticmethod
    async def query_reader(pb: aioredis.client.PubSub):
        msg_list = []
        async for msg in pb.listen():
            # print(f"query_reader msg: {msg}")
            msg_dict = json.loads(msg['data'])
            if 'empty' in msg_dict:
                if msg_dict['empty'] is False:
                    msg_list.append(msg_dict)
            else:
                msg_list.append(msg_dict)
            if 'bIsLast' not in msg_dict or msg_dict['bIsLast']:
                return msg_list

    async def query(self, query_type: str, **kwargs):
        sub_client = None
        channel_rsp_qry, channel_rsp_err = None, None
        try:
            sub_client = self.redis_client.pubsub(ignore_subscribe_messages=True)
            request_id = self.next_id()
            kwargs['RequestID'] = request_id
            channel_rsp_qry = self.__trade_response_format.format('OnRspQry' + query_type, request_id)
            channel_rsp_err = self.__trade_response_format.format('OnRspError', request_id)
            await sub_client.psubscribe(channel_rsp_qry, channel_rsp_err)
            task = asyncio.create_task(self.query_reader(sub_client))
            self.raw_redis.publish(self.__request_format.format('ReqQry' + query_type), json.dumps(kwargs))
            await asyncio.wait_for(task, HANDLER_TIME_OUT)
            await sub_client.punsubscribe()
            await sub_client.close()
            return task.result()

        except Exception as e:
            logger.error('%s failed: %s', query_type, repr(e), exc_info=True)
            if sub_client and channel_rsp_qry:
                await sub_client.unsubscribe()
                await sub_client.close()
            return None

    async def SubscribeMarketData(self, inst_ids: list):
        sub_client = None
        channel_rsp_dat, channel_rsp_err = None, None
        try:
            sub_client = self.redis_client.pubsub(ignore_subscribe_messages=True)
            channel_rsp_dat = self.__market_response_format.format('OnRspSubMarketData', 0)
            channel_rsp_err = self.__market_response_format.format('OnRspError', 0)
            await sub_client.psubscribe(channel_rsp_dat, channel_rsp_err)
            task = asyncio.create_task(self.query_reader(sub_client))
            self.raw_redis.publish(self.__request_format.format('SubscribeMarketData'), json.dumps(inst_ids))
            await asyncio.wait_for(task, HANDLER_TIME_OUT)
            await sub_client.punsubscribe()
            await sub_client.close()
            return task.result()

        except Exception as e:
            logger.error('SubscribeMarketData failed: %s', repr(e), exc_info=True)
            if sub_client and sub_client.in_pubsub and channel_rsp_dat:
                await sub_client.unsubscribe()
                await sub_client.close()
            return None

    async def UnSubscribeMarketData(self, inst_ids: list):
        sub_client = None
        channel_rsp_dat, channel_rsp_err = None, None
        try:
            sub_client = self.redis_client.pubsub(ignore_subscribe_messages=True)
            channel_rsp_dat = self.__market_response_format.format('OnRspUnSubMarketData', 0)
            channel_rsp_err = self.__market_response_format.format('OnRspError', 0)
            await sub_client.psubscribe(channel_rsp_dat, channel_rsp_err)
            task = asyncio.create_task(self.query_reader(sub_client))
            self.raw_redis.publish(self.__request_format.format('UnSubscribeMarketData'), json.dumps(inst_ids))
            await asyncio.wait_for(task, HANDLER_TIME_OUT)
            await sub_client.punsubscribe()
            await sub_client.close()
            return task.result()

        except Exception as e:
            logger.error('UnSubscribeMarketData failed: %s', repr(e), exc_info=True)
            if sub_client and sub_client.in_pubsub and channel_rsp_dat:
                await sub_client.unsubscribe()
                await sub_client.close()
            return None

    async def buy(self, inst: Instrument, limit_price: Decimal, volume: int):
        rst = await self.ReqOrderInsert(
            InstrumentID=inst.main_code,
            VolumeTotalOriginal=volume,
            LimitPrice=float(limit_price),
            Direction=ApiStruct.D_Buy,  # 买
            CombOffsetFlag=ApiStruct.OF_Open,  # 开
            ContingentCondition=ApiStruct.CC_Immediately,  # 立即
            TimeCondition=ApiStruct.TC_GFD)  # 当日有效
        return rst

    async def sell(self, pos: Trade, limit_price: Decimal, volume: int):
        # 上期所区分平今和平昨
        close_flag = ApiStruct.OF_Close
        if pos.open_time.date() == datetime.datetime.today().replace(tzinfo=pytz.FixedOffset(480)).date() \
                and pos.instrument.exchange == ExchangeType.SHFE:
            close_flag = ApiStruct.OF_CloseToday
        rst = await self.ReqOrderInsert(
            InstrumentID=pos.code,
            VolumeTotalOriginal=volume,
            LimitPrice=float(limit_price),
            Direction=ApiStruct.D_Sell,  # 卖
            CombOffsetFlag=close_flag,  # 平
            ContingentCondition=ApiStruct.CC_Immediately,  # 立即
            TimeCondition=ApiStruct.TC_GFD)  # 当日有效
        return rst

    async def sell_short(self, inst: Instrument, limit_price: Decimal, volume: int):
        rst = await self.ReqOrderInsert(
            InstrumentID=inst.main_code,
            VolumeTotalOriginal=volume,
            LimitPrice=float(limit_price),
            Direction=ApiStruct.D_Sell,  # 卖
            CombOffsetFlag=ApiStruct.OF_Open,  # 开
            ContingentCondition=ApiStruct.CC_Immediately,  # 立即
            TimeCondition=ApiStruct.TC_GFD)  # 当日有效
        return rst

    async def buy_cover(self, pos: Trade, limit_price: Decimal, volume: int):
        # 上期所区分平今和平昨
        close_flag = ApiStruct.OF_Close
        if pos.open_time.date() == datetime.datetime.today().replace(tzinfo=pytz.FixedOffset(480)).date() \
                and pos.instrument.exchange == ExchangeType.SHFE:
            close_flag = ApiStruct.OF_CloseToday
        rst = await self.ReqOrderInsert(
            InstrumentID=pos.code,
            VolumeTotalOriginal=volume,
            LimitPrice=float(limit_price),
            Direction=ApiStruct.D_Buy,  # 买
            CombOffsetFlag=close_flag,  # 平
            ContingentCondition=ApiStruct.CC_Immediately,  # 立即
            TimeCondition=ApiStruct.TC_GFD)  # 当日有效
        return rst

    async def ReqOrderInsert(self, **kwargs):
        """
        InstrumentID 合约
        VolumeTotalOriginal 手数
        LimitPrice 限价
        StopPrice 止损价
        Direction 方向
        CombOffsetFlag 开,平,平昨
        ContingentCondition 触发条件
        TimeCondition 持续时间
        """
        sub_client = None
        channel_rtn_odr, channel_rsp_err = None, None
        try:
            sub_client = self.redis_client.pubsub(ignore_subscribe_messages=True)
            request_id = self.next_id()
            order_ref = self.next_order_ref()
            kwargs['nRequestId'] = request_id
            kwargs['OrderRef'] = order_ref
            channel_rtn_odr = self.__trade_response_format.format('OnRtnOrder', order_ref)
            channel_rsp_err = self.__trade_response_format.format('OnRspError', request_id)
            channel_rsp_odr = self.__trade_response_format.format('OnRspOrderInsert', 0)
            await sub_client.psubscribe(channel_rtn_odr, channel_rsp_err, channel_rsp_odr)
            task = asyncio.create_task(self.query_reader(sub_client))
            self.raw_redis.publish(self.__request_format.format('ReqOrderInsert'), json.dumps(kwargs))
            await asyncio.wait_for(task, HANDLER_TIME_OUT)
            await sub_client.punsubscribe()
            await sub_client.close()
            logger.info('ReqOrderInsert, rst: %s', task.result())
            return task.result()
        except Exception as e:
            logger.error('ReqOrderInsert failed: %s', repr(e), exc_info=True)
            if sub_client and sub_client.in_pubsub and channel_rtn_odr:
                await sub_client.unsubscribe()
                await sub_client.close()
            return None

    async def cancel_order(self, order: dict):
        sub_client = None
        channel_rsp_odr_act, channel_rsp_err = None, None
        try:
            sub_client = self.redis_client.pubsub(ignore_subscribe_messages=True)
            request_id = self.next_id()
            order['nRequestID'] = request_id
            channel_rtn_odr = self.__trade_response_format.format('OnRtnOrder', order['OrderRef'])
            channel_rsp_odr_act = self.__trade_response_format.format('OnRspOrderAction', 0)
            channel_rsp_err = self.__trade_response_format.format('OnRspError', request_id)
            await sub_client.psubscribe(channel_rtn_odr, channel_rsp_odr_act, channel_rsp_err)
            task = asyncio.create_task(self.query_reader(sub_client))
            self.raw_redis.publish(self.__request_format.format('ReqOrderAction'), json.dumps(order))
            await asyncio.wait_for(task, HANDLER_TIME_OUT)
            await sub_client.punsubscribe()
            await sub_client.close()
            result = task.result()[0]
            if 'ErrorID' in result:
                logger.error(f"撤销订单出错: ErrorID={result['ErrorID']}")
                return False
            return True
        except Exception as e:
            logger.error('cancel_order failed: %s', repr(e), exc_info=True)
            if sub_client and sub_client.in_pubsub and channel_rsp_odr_act:
                await sub_client.unsubscribe()
                await sub_client.close()
            return None

    @param_function(channel='MSG:CTP:RSP:MARKET:OnRtnDepthMarketData:*')
    async def OnRtnDepthMarketData(self, channel, tick: dict):
        """
        'PreOpenInterest': 50990,
        'TradingDay': '20160803',
        'SettlementPrice': 1.7976931348623157e+308,
        'AskVolume1': 40,
        'Volume': 11060,
        'LastPrice': 37740,
        'LowestPrice': 37720,
        'ClosePrice': 1.7976931348623157e+308,
        'ActionDay': '20160803',
        'UpdateMillisec': 0,
        'PreClosePrice': 37840,
        'LowerLimitPrice': 35490,
        'OpenInterest': 49460,
        'UpperLimitPrice': 40020,
        'AveragePrice': 189275.7233273056,
        'HighestPrice': 38230,
        'BidVolume1': 10,
        'UpdateTime': '11:03:12',
        'InstrumentID': 'cu1608',
        'PreSettlementPrice': 37760,
        'OpenPrice': 37990,
        'BidPrice1': 37740,
        'Turnover': 2093389500,
        'AskPrice1': 37750
        """
        try:
            inst = channel.split(':')[-1]
            tick['UpdateTime'] = datetime.datetime.strptime(tick['UpdateTime'], "%Y%m%d %H:%M:%S:%f")
            logger.info('inst=%s, tick: %s', inst, tick)
        except Exception as ee:
            logger.error('OnRtnDepthMarketData failed: %s', repr(ee), exc_info=True)

    # TODO 有时更新仓位计算会出现错误，导致同样的品种头寸被插入而不是更新
    @param_function(channel='MSG:CTP:RSP:TRADE:OnRtnTrade:*')
    async def OnRtnTrade(self, channel, trade: dict):
        try:
            signal = None
            order_ref = channel.split(':')[-1]
            logger.info(f'OnRtnTrade order_ref: {order_ref}, trade: {trade}')
            inst = Instrument.objects.get(product_code=re.findall('[A-Za-z]+', trade['InstrumentID'])[0])
            order = Order.objects.filter(order_ref=order_ref).first()
            if trade['OffsetFlag'] == ApiStruct.OF_Open:
                last_trade = Trade.objects.filter(
                    broker=self.__broker, strategy=self.__strategy, instrument=inst,
                    code=trade['InstrumentID'],
                    open_time__startswith='{}-{}-{}'.format(
                        trade['TradingDay'][0:4], trade['TradingDay'][4:6], trade['TradingDay'][6:8]),
                    direction=DirectionType.LONG if trade['Direction'] == ApiStruct.D_Buy
                    else DirectionType.SHORT, close_time__isnull=True).first()
                if last_trade is None:
                    last_trade = Trade.objects.create(
                        broker=self.__broker, strategy=self.__strategy, instrument=inst,
                        code=trade['InstrumentID'], open_order=order,
                        direction=DirectionType.LONG if trade['Direction'] == ApiStruct.D_Buy
                        else DirectionType.SHORT,
                        open_time=datetime.datetime.strptime(
                            trade['TradeDate']+trade['TradeTime'], '%Y%m%d%H:%M:%S').replace(
                            tzinfo=pytz.FixedOffset(480)),
                        shares=order.volume if order is not None else trade['Volume'],
                        filled_shares=trade['Volume'], avg_entry_price=trade['Price'],
                        cost=trade['Volume'] * Decimal(trade['Price']) * inst.fee_money *
                        inst.volume_multiple + trade['Volume'] * inst.fee_volume,
                        frozen_margin=trade['Volume'] * Decimal(trade['Price']) * inst.margin_rate)
                else:
                    last_trade.avg_entry_price = \
                        (last_trade.avg_entry_price * last_trade.filled_shares + trade['Volume'] *
                         trade['Price']) / (last_trade.filled_shares + trade['Volume'])
                    last_trade.filled_shares += trade['Volume']
                    last_trade.cost += \
                        trade['Volume'] * Decimal(trade['Price']) * inst.fee_money * \
                        inst.volume_multiple + trade['Volume'] * inst.fee_volume
                    last_trade.frozen_margin += trade['Volume'] * Decimal(trade['Price']) * inst.margin_rate
                    last_trade.save()
                signal = Signal.objects.filter(
                    Q(type=SignalType.BUY if trade['Direction'] == ApiStruct.D_Buy
                        else SignalType.SELL_SHORT) | Q(type=SignalType.ROLL_OPEN),
                    code=trade['InstrumentID'], volume=last_trade.filled_shares,
                    strategy=self.__strategy, instrument=inst, processed=False)
            else:
                last_trade = Trade.objects.filter(
                    broker=self.__broker, strategy=self.__strategy, instrument=inst,
                    code=trade['InstrumentID'],
                    direction=DirectionType.LONG if trade['Direction'] == ApiStruct.D_Sell
                    else DirectionType.SHORT, close_time__isnull=True).first()
                if last_trade is not None:
                    if last_trade.closed_shares is None:
                        last_trade.closed_shares = 0
                    if last_trade.avg_exit_price is None:
                        last_trade.avg_exit_price = Decimal(0)
                    last_trade.avg_exit_price = \
                        (last_trade.avg_exit_price * last_trade.closed_shares +
                         trade['Volume'] * Decimal(trade['Price'])) / \
                        (last_trade.closed_shares + trade['Volume'])
                    last_trade.closed_shares += trade['Volume']
                    last_trade.cost += \
                        trade['Volume'] * Decimal(trade['Price']) * inst.fee_money * \
                        inst.volume_multiple + trade['Volume'] * inst.fee_volume
                    if last_trade.closed_shares == last_trade.shares:
                        # 全部成交
                        last_trade.close_order = order
                        last_trade.close_time = datetime.datetime.strptime(
                            trade['TradeDate']+trade['TradeTime'], '%Y%m%d%H:%M:%S').replace(
                            tzinfo=pytz.FixedOffset(480))
                        last_trade.profit = (last_trade.avg_exit_price - last_trade.avg_entry_price) * \
                            last_trade.shares * inst.volume_multiple
                    last_trade.save()
                    signal = Signal.objects.filter(
                        Q(type=SignalType.BUY_COVER if trade['Direction'] == ApiStruct.D_Buy
                            else SignalType.SELL) | Q(type=SignalType.ROLL_CLOSE),
                        code=trade['InstrumentID'], volume=last_trade.closed_shares,
                        strategy=self.__strategy, instrument=inst, processed=False)
            if signal is not None:
                signal.update(processed=True)
        except Exception as ee:
            logger.error('OnRtnTrade failed: %s', repr(ee), exc_info=True)

    @param_function(channel='MSG:CTP:RSP:TRADE:OnRtnOrder:*')
    async def OnRtnOrder(self, channel, order: dict):
        try:
            order_ref = channel.split(':')[-1]
            logger.info(f'OnRtnOrder order_ref: {order_ref}, order: {order}')
            inst = Instrument.objects.get(product_code=re.findall('[A-Za-z]+', order['InstrumentID'])[0])
            Order.objects.update_or_create(order_ref=order_ref, defaults={
                'broker': self.__broker, 'strategy': self.__strategy, 'instrument': inst,
                'code': order['InstrumentID'], 'front': order['FrontID'], 'session': order['SessionID'],
                'price': order['LimitPrice'], 'volume': order['VolumeTotalOriginal'],
                'direction': DirectionType.LONG if order['Direction'] == ApiStruct.D_Buy else DirectionType.SHORT,
                'offset_flag': OffsetFlag.OPEN if order['CombOffsetFlag'] == ApiStruct.OF_Open else OffsetFlag.CLOSE,
                'status': order['OrderStatus'],
                'send_time': datetime.datetime.strptime(
                    order['InsertDate']+order['InsertTime'], '%Y%m%d%H:%M:%S').replace(
                    tzinfo=pytz.FixedOffset(480)),
                'update_time': datetime.datetime.now().replace(tzinfo=pytz.FixedOffset(480))})
            # 处理由于委托价格超出交易所涨跌停板而被撤单的报单，将委托价格下调80%，重新报单
            # if order['OrderStatus'] == ApiStruct.OST_Canceled:
            #     last_bar = DailyBar.objects.filter(
            #         exchange=inst.exchange, code=order['InstrumentID']).order_by('-time').first()
            #     volume = int(order['VolumeTotalOriginal'])
            #     if order['CombOffsetFlag'] == ApiStruct.OF_Open:
            #         if order['Direction'] == ApiStruct.D_Buy:
            #             new_price = (Decimal(order['LimitPrice']) - last_bar.settlement) * Decimal(0.8)
            #             if new_price / last_bar.settlement < 0.01:
            #                 logger.info('%s %s 报单重试次数过多， 放弃。', order['InstrumentID'], new_price)
            #                 return
            #             new_price = price_round(last_bar.settlement + new_price, inst.price_tick)
            #             logger.info('%s 重新尝试开多%s手 价格: %s', inst, volume, new_price)
            #             self.io_loop.create_task(self.buy(inst, new_price, volume))
            #         else:
            #             new_price = (last_bar.settlement - Decimal(order['LimitPrice'])) * Decimal(0.8)
            #             if new_price / last_bar.settlement < 0.01:
            #                 logger.info('%s %s 报单重试次数过多， 放弃。', order['InstrumentID'], new_price)
            #                 return
            #             new_price = price_round(last_bar.settlement - new_price, inst.price_tick)
            #             logger.info('%s 重新尝试开空%s手 价格: %s', inst, volume, new_price)
            #             self.io_loop.create_task(self.sell_short(inst, new_price, volume))
            #     else:
            #         pos = Trade.objects.filter(
            #             close_time__isnull=True, code=order['InstrumentID'], broker=self.__broker,
            #             strategy=self.__strategy, instrument=inst, shares__gt=0).first()
            #         if order['Direction'] == ApiStruct.D_Buy:
            #             new_price = (Decimal(order['LimitPrice']) - last_bar.settlement) * Decimal(0.8)
            #             if new_price / last_bar.settlement < 0.01:
            #                 logger.info('%s %s 报单重试次数过多， 放弃。', order['InstrumentID'], new_price)
            #                 return
            #             new_price = price_round(last_bar.settlement + new_price, inst.price_tick)
            #             logger.info('%s 重新尝试买平%s手 价格: %s', inst, volume, new_price)
            #             self.io_loop.create_task(self.buy_cover(pos, new_price, volume))
            #         else:
            #             new_price = (last_bar.settlement - Decimal(order['LimitPrice'])) * Decimal(0.8)
            #             if new_price / last_bar.settlement < 0.01:
            #                 logger.info('%s %s 报单重试次数过多， 放弃。', order['InstrumentID'], new_price)
            #                 return
            #             new_price = price_round(last_bar.settlement - new_price, inst.price_tick)
            #             logger.info('%s 重新尝试卖平%s手 价格: %s', inst, volume, new_price)
            #             self.io_loop.create_task(self.sell(pos, new_price, volume))
        except Exception as ee:
            logger.error('OnRtnOrder failed: %s', repr(ee), exc_info=True)

    @param_function(channel='MSG:CTP:RSP:TRADE:OnRspQryInvestorPositionDetail:*')
    async def OnRspQryInvestorPositionDetail(self, _, pos: dict):
        if 'empty' in pos and pos['empty'] is True:
            return
        if pos['Volume'] > 0:
            old_pos = self.__shares.get(pos['InstrumentID'])
            if old_pos is None:
                self.__shares[pos['InstrumentID']] = pos
            else:
                old_pos['OpenPrice'] = (old_pos['OpenPrice'] * old_pos['Volume'] +
                                        pos['OpenPrice'] * pos['Volume']) / (old_pos['Volume'] + pos['Volume'])
                old_pos['Volume'] += pos['Volume']
                old_pos['PositionProfitByTrade'] += pos['PositionProfitByTrade']
                old_pos['Margin'] += pos['Margin']
        if pos['bIsLast']:
            self.update_position()

    @param_function(channel='MSG:CTP:RSP:TRADE:OnRspQryTradingAccount:*')
    async def OnRspQryTradingAccount(self, _, account: dict):
        self.update_account(account)

#     @param_function(channel='MSG:CTP:RSP:TRADE:OnRtnInstrumentStatus:*')
#     async def OnRtnInstrumentStatus(self, channel, status: dict):
#         """
# {"EnterReason":"1","EnterTime":"10:30:00","ExchangeID":"SHFE","ExchangeInstID":"ru","InstrumentID":"ru",
#  "InstrumentStatus":"2","SettlementGroupID":"00000001","TradingSegmentSN":27}
#         """
#         try:
#             product_code = channel.split(':')[-1]
#             inst = self.__strategy.instruments.filter(product_code=product_code).first()
#             if inst is None or product_code not in self.__inst_ids:
#                 return
#             # logger.info('合约状态通知: %s %s', inst, status)
#             if is_auction_time(inst, status):
#                 logger.info('%s 开始集合竞价, 查询待处理信号..', inst)
#                 self.process_signal(inst)
#         except Exception as ee:
#             logger.error('OnRtnInstrumentStatus failed: %s', repr(ee), exc_info=True)

    @param_function(crontab='*/1 * * * *')
    async def heartbeat(self):
        self.raw_redis.set('HEARTBEAT:TRADER', 1, ex=301)

    @param_function(crontab='55 8 * * *')
    async def processing_signal1(self):
        day = datetime.datetime.today()
        day = day.replace(tzinfo=pytz.FixedOffset(480))
        _, trading = await is_trading_day(day)
        if trading:
            logger.info('查询日盘信号..')
            for sig in Signal.objects.filter(
                    ~Q(instrument__exchange=ExchangeType.CFFEX),
                    strategy=self.__strategy, instrument__night_trade=False, processed=False).all():
                logger.info('发现日盘信号: %s', sig)
                self.process_signal(sig)

    @param_function(crontab='1 9 * * *')
    async def check_signal1_processed(self):
        day = datetime.datetime.today()
        day = day.replace(tzinfo=pytz.FixedOffset(480))
        _, trading = await is_trading_day(day)
        if trading:
            logger.info('查询遗漏的日盘信号..')
            for sig in Signal.objects.filter(
                    ~Q(instrument__exchange=ExchangeType.CFFEX),
                    strategy=self.__strategy, instrument__night_trade=False, processed=False).all():
                logger.info('发现遗漏信号: %s', sig)
                self.process_signal(sig)

    @param_function(crontab='25 9 * * *')
    async def processing_signal2(self):
        day = datetime.datetime.today()
        day = day.replace(tzinfo=pytz.FixedOffset(480))
        _, trading = await is_trading_day(day)
        if trading:
            logger.info('查询股指和国债信号..')
            for sig in Signal.objects.filter(
                    instrument__exchange=ExchangeType.CFFEX,
                    strategy=self.__strategy, instrument__night_trade=False, processed=False).all():
                logger.info('发现股指和国债信号: %s', sig)
                self.process_signal(sig)

    @param_function(crontab='31 9 * * *')
    async def check_signal2_processed(self):
        day = datetime.datetime.today()
        day = day.replace(tzinfo=pytz.FixedOffset(480))
        _, trading = await is_trading_day(day)
        if trading:
            logger.info('查询遗漏的股指和国债信号..')
            for sig in Signal.objects.filter(
                    instrument__exchange=ExchangeType.CFFEX,
                    strategy=self.__strategy, instrument__night_trade=False, processed=False).all():
                logger.info('发现股指和国债信号: %s', sig)
                self.process_signal(sig)

    @param_function(crontab='55 20 * * *')
    async def processing_signal3(self):
        day = datetime.datetime.today()
        day = day.replace(tzinfo=pytz.FixedOffset(480))
        _, trading = await is_trading_day(day)
        if trading:
            logger.info('查询夜盘信号..')
            for sig in Signal.objects.filter(
                    strategy=self.__strategy, instrument__night_trade=True, processed=False).all():
                logger.info('发现夜盘信号: %s', sig)
                self.process_signal(sig)

    @param_function(crontab='1 21 * * *')
    async def check_signal3_processed(self):
        day = datetime.datetime.today()
        day = day.replace(tzinfo=pytz.FixedOffset(480))
        _, trading = await is_trading_day(day)
        if trading:
            logger.info('查询遗漏的夜盘信号..')
            for sig in Signal.objects.filter(
                    strategy=self.__strategy, instrument__night_trade=True, processed=False).all():
                logger.info('发现遗漏信号: %s', sig)
                self.process_signal(sig)

    @param_function(crontab='20 15 * * *')
    async def refresh_instrument(self):
        day = datetime.datetime.today().replace(tzinfo=pytz.FixedOffset(480))
        _, trading = await is_trading_day(day)
        if not trading:
            logger.info('今日是非交易日, 不更新任何数据。')
            return
        logger.info('更新账户')
        await self.query('TradingAccount')
        logger.info('更新持仓')
        self.__shares.clear()
        await self.query('InvestorPositionDetail')
        logger.info('更新合约数据..')
        inst_dict = defaultdict(dict)
        inst_list = await self.query('Instrument')
        for inst in inst_list:
            if not inst['empty']:
                if inst['IsTrading'] == 1 and inst['ProductClass'] == ApiStruct.PC_Futures and inst['StrikePrice'] == 0:
                    if inst['ProductID'] in self.__ignore_inst_list or inst['LongMarginRatio'] > 1:
                        continue
                    inst_dict[inst['ProductID']][inst['InstrumentID']] = dict()
                    inst_dict[inst['ProductID']][inst['InstrumentID']]['name'] = inst['InstrumentName']
                    inst_dict[inst['ProductID']][inst['InstrumentID']]['exchange'] = inst['ExchangeID']
                    inst_dict[inst['ProductID']][inst['InstrumentID']]['multiple'] = inst['VolumeMultiple']
                    inst_dict[inst['ProductID']][inst['InstrumentID']]['price_tick'] = inst['PriceTick']
                    inst_dict[inst['ProductID']][inst['InstrumentID']]['margin'] = inst['LongMarginRatio']
        for code in inst_dict.keys():
            all_inst = ','.join(sorted(inst_dict[code].keys()))
            inst_data = list(inst_dict[code].values())[0]
            valid_name = self.__re_extract_name.match(inst_data['name'])
            if valid_name is not None:
                valid_name = valid_name.group(1)
            else:
                valid_name = inst_data['name']
            if valid_name == code:
                valid_name = ''
            inst_data['name'] = valid_name
            inst, created = Instrument.objects.update_or_create(product_code=code, exchange=inst_data['exchange'])
            if created:
                inst.name = inst_data['name']
                inst.volume_multiple = inst_data['multiple']
                inst.price_tick = inst_data['price_tick']
                inst.margin_rate = inst_data['margin']
                inst.save(update_fields=['name', 'volume_multiple', 'price_tick', 'margin_rate'])
            elif inst.main_code:
                inst.margin_rate = inst_dict[code][inst.main_code]['margin']
                inst.all_inst = all_inst
                inst.save(update_fields=['margin_rate', 'all_inst'])
                await self.update_inst_fee(inst)
        logger.info('更新合约列表完成!')

    @param_function(crontab='0 17 * * *')
    async def collect_quote(self):
        """
        各品种的主联合约：计算基差，主联合约复权（新浪）
        资金曲线（ctp）
        各品种换月标志
        各品种开仓价格
        各品种平仓价格
        微信报告
        """
        try:
            day = datetime.datetime.today()
            day = day.replace(tzinfo=pytz.FixedOffset(480))
            _, trading = await is_trading_day(day)
            if not trading:
                logger.info('今日是非交易日, 不计算任何数据。')
                return
            logger.info('每日盘后计算, day: %s, 获取交易所日线数据..', day)
            tasks = [
                self.io_loop.create_task(update_from_shfe(day)),
                self.io_loop.create_task(update_from_dce(day)),
                self.io_loop.create_task(update_from_czce(day)),
                self.io_loop.create_task(update_from_cffex(day)),
            ]
            await asyncio.wait(tasks)
            logger.info('获取合约涨跌停幅度...')
            await get_contracts_argument(day)
            for inst_obj in Instrument.objects.all():
                logger.info('计算连续合约, 交易信号: %s', inst_obj.name)
                calc_main_inst(inst_obj, day)
                self.calc_signal(inst_obj, day)
        except Exception as e:
            logger.error('collect_quote failed: %s', e, exc_info=True)
        logger.info('盘后计算完毕!')

    # @param_function(crontab='57 8 * * *')
    async def collect_day_tick_start(self):
        day = datetime.datetime.today().replace(tzinfo=pytz.FixedOffset(480))
        day, trading = await is_trading_day(day)
        if trading:
            logger.info('订阅全品种行情, %s %s', day, trading)
            inst_set = list()
            for inst in Instrument.objects.all():
                inst_set += inst.all_inst.split(',')
            await self.SubscribeMarketData(inst_set)

    # @param_function(crontab='16 15 * * *')
    async def collect_day_tick_stop(self):
        day = datetime.datetime.today().replace(tzinfo=pytz.FixedOffset(480))
        day, trading = await is_trading_day(day)
        if trading:
            logger.info('取消订阅全品种行情, %s %s', day, trading)
            inst_set = list()
            for inst in Instrument.objects.all():
                inst_set += inst.all_inst.split(',')
            await self.UnSubscribeMarketData(inst_set)

    # @param_function(crontab='57 20 * * *')
    async def collect_night_tick_start(self):
        day = datetime.datetime.today().replace(tzinfo=pytz.FixedOffset(480))
        day, trading = await is_trading_day(day)
        if trading:
            logger.info('订阅全品种行情, %s %s', day, trading)
            inst_set = list()
            for inst in Instrument.objects.all():
                inst_set += inst.all_inst.split(',')
            await self.SubscribeMarketData(inst_set)

    # @param_function(crontab='31 2 * * *')
    async def collect_night_tick_stop(self):
        day = datetime.datetime.today().replace(tzinfo=pytz.FixedOffset(480))
        day, trading = await is_trading_day(day)
        if trading:
            logger.info('取消订阅全品种行情, %s %s', day, trading)
            inst_set = list()
            for inst in Instrument.objects.all():
                inst_set += inst.all_inst.split(',')
            await self.UnSubscribeMarketData(inst_set)

    @param_function(crontab='30 15 * * *')
    async def update_equity(self):
        today, trading = await is_trading_day(datetime.datetime.today().replace(tzinfo=pytz.FixedOffset(480)))
        if trading:
            logger.info('更新资金净值 %s %s', today, trading)
            dividend = Performance.objects.filter(
                broker=self.__broker, day__lt=today.date()).aggregate(Sum('dividend'))['dividend__sum']
            if dividend is None:
                dividend = Decimal(0)
            perform = Performance.objects.filter(
                broker=self.__broker, day__lt=today.date()).order_by('-day').first()
            if perform is None:
                unit = Decimal(1000000)
            else:
                unit = perform.unit_count
            nav = (self.__current + self.__fake) / unit
            accumulated = (self.__current + self.__fake - dividend) / (unit - dividend)
            Performance.objects.update_or_create(broker=self.__broker, day=today.date(), defaults={
                'used_margin': self.__margin,
                'capital': self.__current, 'unit_count': unit, 'NAV': nav, 'accumulated': accumulated})

    async def update_inst_fee(self, inst: Instrument):
        """
        更新每一个合约的手续费
        """
        try:
            fee = await self.query('InstrumentCommissionRate', InstrumentID=inst.main_code)
            fee = fee[0]
            inst.fee_money = Decimal(fee['CloseRatioByMoney'])
            inst.fee_volume = Decimal(fee['CloseRatioByVolume'])
            inst.save(update_fields=['fee_money', 'fee_volume'])
            logger.info(f"{inst} 已更新手续费")
        except Exception as e:
            logger.error('update_inst_fee failed: %s', e, exc_info=True)

    def calc_signal(self, inst: Instrument, day: datetime.datetime):
        try:
            if inst.product_code not in self.__inst_ids:
                return
            break_n = self.__strategy.param_set.get(code='BreakPeriod').int_value
            atr_n = self.__strategy.param_set.get(code='AtrPeriod').int_value
            long_n = self.__strategy.param_set.get(code='LongPeriod').int_value
            short_n = self.__strategy.param_set.get(code='ShortPeriod').int_value
            stop_n = self.__strategy.param_set.get(code='StopLoss').int_value
            risk = self.__strategy.param_set.get(code='Risk').float_value
            df = to_df(MainBar.objects.filter(
                time__lte=day.date(),
                exchange=inst.exchange, product_code=inst.product_code).order_by('time').values_list(
                'time', 'open', 'high', 'low', 'close', 'settlement'))
            df.index = pd.DatetimeIndex(df.time)
            df['atr'] = ATR(df.open, df.high, df.low, timeperiod=atr_n)
            df['short_trend'] = df.close
            df['long_trend'] = df.close
            # df columns: 0:time,1:open,2:high,3:low,4:close,5:settlement,6:atr,7:short_trend,8:long_trend
            for idx in range(1, df.shape[0]):
                df.iloc[idx, 7] = (df.iloc[idx - 1, 7] * (short_n - 1) + df.iloc[idx, 4]) / short_n
                df.iloc[idx, 8] = (df.iloc[idx - 1, 8] * (long_n - 1) + df.iloc[idx, 4]) / long_n
            df['high_line'] = df.close.rolling(window=break_n).max()
            df['low_line'] = df.close.rolling(window=break_n).min()
            idx = -1
            pos_idx = None
            buy_sig = df.short_trend[idx] > df.long_trend[idx] and int(df.close[idx]) >= int(df.high_line[idx - 1])
            sell_sig = df.short_trend[idx] < df.long_trend[idx] and int(df.close[idx]) <= int(df.low_line[idx - 1])
            pos = Trade.objects.filter(
                Q(close_time__isnull=True) | Q(close_time__gt=day),
                broker=self.__broker, strategy=self.__strategy,
                instrument=inst, shares__gt=0, open_time__lt=day).first()
            roll_over = False
            if pos is not None:
                pos_idx = df.index.get_loc(
                    pos.open_time.astimezone(pytz.FixedOffset(480)).date().isoformat())
                roll_over = pos.code != inst.main_code
            elif self.__strategy.force_opens.filter(id=inst.id).exists() and not buy_sig and not sell_sig:
                logger.info('强制开仓: %s', inst)
                if df.short_trend[idx] > df.long_trend[idx]:
                    buy_sig = True
                else:
                    sell_sig = True
                self.__strategy.force_opens.remove(inst)
            signal = None
            signal_value = None
            price = None
            volume = None
            if pos is not None:
                # 多头持仓
                if pos.direction == DirectionType.LONG:
                    hh = float(MainBar.objects.filter(
                        exchange=inst.exchange, product_code=pos.instrument.product_code,
                        time__gte=pos.open_time.date(), time__lte=day).aggregate(Max('high'))['high__max'])
                    # 多头止损
                    if df.close[idx] <= hh - df.atr[pos_idx - 1] * stop_n:
                        signal = SignalType.SELL
                        # 止损时 signal_value 为止损价
                        signal_value = hh - df.atr[pos_idx - 1] * stop_n
                        volume = pos.shares
                        last_bar = DailyBar.objects.filter(
                            exchange=inst.exchange, code=pos.code, time=day.date()).first()
                        price = self.calc_down_limit(inst, last_bar)
                    # 多头换月
                    elif roll_over:
                        signal = SignalType.ROLL_OPEN
                        volume = pos.shares
                        last_bar = DailyBar.objects.filter(
                            exchange=inst.exchange, code=pos.code, time=day.date()).first()
                        # 换月时 signal_value 为旧合约的平仓价
                        signal_value = self.calc_down_limit(inst, last_bar)
                        new_bar = DailyBar.objects.filter(
                            exchange=inst.exchange, code=inst.main_code, time=day.date()).first()
                        price = self.calc_up_limit(inst, new_bar)
                        Signal.objects.update_or_create(
                            code=pos.code, strategy=self.__strategy, instrument=inst,
                            type=SignalType.ROLL_CLOSE, trigger_time=day, defaults={
                                'price': signal_value, 'volume': volume,
                                'priority': PriorityType.Normal, 'processed': False})
                # 空头持仓
                else:
                    ll = float(MainBar.objects.filter(
                        exchange=inst.exchange, product_code=pos.instrument.product_code,
                        time__gte=pos.open_time.date(), time__lte=day).aggregate(Min('low'))['low__min'])
                    # 空头止损
                    if df.close[idx] >= ll + df.atr[pos_idx - 1] * stop_n:
                        signal = SignalType.BUY_COVER
                        signal_value = ll + df.atr[pos_idx - 1] * stop_n
                        volume = pos.shares
                        last_bar = DailyBar.objects.filter(
                            exchange=inst.exchange, code=pos.code, time=day.date()).first()
                        price = self.calc_up_limit(inst, last_bar)
                    # 空头换月
                    elif roll_over:
                        signal = SignalType.ROLL_OPEN
                        volume = pos.shares
                        last_bar = DailyBar.objects.filter(
                            exchange=inst.exchange, code=pos.code, time=day.date()).first()
                        signal_value = self.calc_up_limit(inst, last_bar)
                        new_bar = DailyBar.objects.filter(
                            exchange=inst.exchange, code=inst.main_code, time=day.date()).first()
                        price = self.calc_down_limit(inst, new_bar)
                        Signal.objects.update_or_create(
                            code=pos.code, strategy=self.__strategy, instrument=inst,
                            type=SignalType.ROLL_CLOSE, trigger_time=day, defaults={
                                'price': signal_value, 'volume': volume,
                                'priority': PriorityType.Normal, 'processed': False})
            # 做多
            elif buy_sig:
                volume = (self.__current + self.__fake) * risk // \
                         (Decimal(df.atr[idx]) * Decimal(inst.volume_multiple))
                if volume > 0:
                    signal = SignalType.BUY
                    signal_value = df.high_line[idx - 1]
                    new_bar = DailyBar.objects.filter(
                        exchange=inst.exchange, code=inst.main_code, time=day.date()).first()
                    price = self.calc_up_limit(inst, new_bar)
                else:
                    logger.info('做多单手风险=%s,超出风控额度，放弃。', df.atr[idx] * inst.volume_multiple)
            # 做空
            elif sell_sig:
                volume = (self.__current + self.__fake) * risk // \
                         (Decimal(df.atr[idx]) * Decimal(inst.volume_multiple))
                if volume > 0:
                    signal = SignalType.SELL_SHORT
                    signal_value = df.low_line[idx - 1]
                    new_bar = DailyBar.objects.filter(
                        exchange=inst.exchange, code=inst.main_code, time=day.date()).first()
                    price = self.calc_down_limit(inst, new_bar)
                else:
                    logger.info('做空单手风险=%s,超出风控额度，放弃。', df.atr[idx] * inst.volume_multiple)
            if signal is not None:
                Signal.objects.update_or_create(
                    code=inst.main_code,
                    strategy=self.__strategy, instrument=inst, type=signal, trigger_time=day, defaults={
                        'price': price, 'volume': volume, 'trigger_value': signal_value,
                        'priority': PriorityType.Normal, 'processed': False})
        except Exception as e:
            logger.error('calc_signal failed: %s', e, exc_info=True)

    def process_signal(self, signal: Signal):
        """
        :param signal: 信号
        :return: None
        """
        price = signal.price
        inst = signal.instrument
        if signal.type == SignalType.BUY:
            logger.info('%s 开多%s手 价格: %s', inst, signal.volume, price)
            self.io_loop.create_task(self.buy(inst, price, signal.volume))
        elif signal.type == SignalType.SELL_SHORT:
            logger.info('%s 开空%s手 价格: %s', inst, signal.volume, price)
            self.io_loop.create_task(self.sell_short(inst, price, signal.volume))
        elif signal.type == SignalType.BUY_COVER:
            pos = Trade.objects.filter(
                broker=self.__broker, strategy=self.__strategy,
                code=signal.code, close_time__isnull=True, direction=DirectionType.SHORT,
                instrument=inst, shares__gt=0).first()
            logger.info('%s 平空%s手 价格: %s', pos.instrument, signal.volume, price)
            self.io_loop.create_task(self.buy_cover(pos, price, signal.volume))
        elif signal.type == SignalType.SELL:
            pos = Trade.objects.filter(
                broker=self.__broker, strategy=self.__strategy,
                code=signal.code, close_time__isnull=True, direction=DirectionType.LONG,
                instrument=inst, shares__gt=0).first()
            logger.info('%s 平多%s手 价格: %s', pos.instrument, signal.volume, price)
            self.io_loop.create_task(self.sell(pos, price, signal.volume))
        elif signal.type == SignalType.ROLL_CLOSE:
            pos = Trade.objects.filter(
                broker=self.__broker, strategy=self.__strategy,
                code=signal.code, close_time__isnull=True, instrument=inst, shares__gt=0).first()
            if pos.direction == DirectionType.LONG:
                logger.info('%s->%s 多头换月平旧%s手 价格: %s', pos.code, inst.main_code, signal.volume, price)
                self.io_loop.create_task(self.sell(pos, price, signal.volume))
            else:
                logger.info('%s->%s 空头换月平旧%s手 价格: %s', pos.code, inst.main_code, signal.volume, price)
                self.io_loop.create_task(self.buy_cover(pos, price, signal.volume))
        elif signal.type == SignalType.ROLL_OPEN:
            pos = Trade.objects.filter(
                Q(close_time__isnull=True) | Q(close_time__startswith=datetime.date.today()),
                broker=self.__broker, strategy=self.__strategy,
                shares=signal.volume, code=inst.last_main, instrument=inst, shares__gt=0).first()
            if pos.direction == DirectionType.LONG:
                logger.info('%s->%s 多头换月开新%s手 价格: %s', pos.code, inst.main_code, signal.volume, price)
                self.io_loop.create_task(self.buy(inst, price, signal.volume))
            else:
                logger.info('%s->%s 空头换月开新%s手 价格: %s', pos.code, inst.main_code, signal.volume, price)
                self.io_loop.create_task(self.sell_short(inst, price, signal.volume))

    async def force_close_all(self) -> bool:
        try:
            logger.info('强制平仓..')
            for trade in Trade.objects.filter(close_time__isnull=True).all():
                shares = trade.filled_shares
                if trade.closed_shares:
                    shares -= trade.closed_shares
                bar = DailyBar.objects.filter(code=trade.code).order_by('-time').first()
                if trade.direction == DirectionType.LONG:
                    await self.sell(trade, self.calc_up_limit(trade.instrument, bar), shares)
                else:
                    await self.buy_cover(trade, self.calc_down_limit(trade.instrument, bar), shares)
        except Exception as e:
            logger.error('force_close_all failed: %s', e, exc_info=True)
            return False
        return True

    def calc_up_limit(self, inst: Instrument, bar: DailyBar):
        settlement = bar.settlement
        limit_ratio = str_to_number(self.raw_redis.get(f"LIMITRATIO:{inst.exchange}:{inst.product_code}:{bar.code}"))
        price_tick = inst.price_tick
        price = price_round(settlement * (Decimal(1) + limit_ratio), price_tick)
        return price - price_tick

    def calc_down_limit(self, inst: Instrument, bar: DailyBar):
        settlement = bar.settlement
        limit_ratio = str_to_number(self.raw_redis.get(f"LIMITRATIO:{inst.exchange}:{inst.product_code}:{bar.code}"))
        price_tick = inst.price_tick
        price = price_round(settlement * (Decimal(1) - limit_ratio), price_tick)
        return price + price_tick
