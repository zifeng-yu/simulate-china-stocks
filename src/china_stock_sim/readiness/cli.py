"""Read-only local shutdown readiness; no business orchestration or observation writes."""

import sys
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from china_stock_sim.config import load_settings

COMMANDS = {'shutdown-status'}


def register_commands(commands):
    commands.add_parser('shutdown-status', help='只读核对当晚日结和下一交易日冻结计划的关机就绪状态')


def execute(args, root):
    try:
        from .status import build_status

        result = build_status(load_settings(root))
        diagnostic = result.get('diagnostic', {})
        if diagnostic.get('stack'):
            for frame in diagnostic['stack'][:24]:
                print(frame, file=sys.stderr)
            print(str(diagnostic.get('exception_type', 'Error')) +
                  ': SHUTDOWN_STATUS_UNAVAILABLE', file=sys.stderr)
            return result, 2
        return result, 0
    except Exception as exc:
        # Preserve fault frames without dumping potentially sensitive exception values.
        for frame in traceback.extract_tb(exc.__traceback__):
            print(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}',
                  file=sys.stderr)
        print(type(exc).__name__ + ': SHUTDOWN_STATUS_UNAVAILABLE', file=sys.stderr)
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        return {'schema_version': 1, 'state': 'UNKNOWN', 'label': '待核查',
                'business_session': None, 'target_session': None,
                'sampled_at': now.isoformat(), 'expires_at': (now + timedelta(seconds=30)).isoformat(),
                'accounts': [], 'reasons': [{'code': 'PROBE_FAILED',
                    'message': '关机条件读取失败，不能确认可关机'}], 'optional_work': []}, 2
