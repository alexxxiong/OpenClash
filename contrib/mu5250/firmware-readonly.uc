'use strict';
import { connect } from 'ubus';

const firmware = connect('/data/openclash/run/host-ubus.sock');
function method(object, name, args) {
    return {
        args: args ?? {},
        call: function(request) {
            return firmware.call(object, name, request.args ?? {}) ?? {};
        }
    };
}

return {
    system: {
        board: method('system', 'board'),
        info: method('system', 'info')
    },
    network: {
        get_proto_handlers: method('network', 'get_proto_handlers')
    },
    'network.interface': {
        dump: method('network.interface', 'dump')
    },
    'network.device': {
        status: method('network.device', 'status', { name: 'name' })
    },
    'network.wireless': {
        status: method('network.wireless', 'status')
    }
};
