import asyncio
from datetime import datetime
import json
import os
import subprocess
from threading import Thread

import boto3
from botocore.exceptions import NoCredentialsError
import websockets


dynamodb = boto3.client('dynamodb', region_name='us-west-2')

# globals
CLIENTS = set()
ec2_credentials_failure = False
ord_command = '/home/ubuntu/ord/target/release/ord --bitcoin-data-dir=/mnt/bitcoin-ord-data/bitcoin --data-dir=/mnt/bitcoin-ord-data/ord'

# get our terraform-generated password (see main.tf)
ourpath = os.path.dirname(os.path.realpath(__file__))
filepath = os.path.join(ourpath, 'client-env.js.txt')
token_file = open(filepath, 'r')
token = token_file.read()
token = token.split('window.OrdServer.password="')[1].split('";')[0]


def _build_dynamo_item(id, name, details):
    now = datetime.utcnow().isoformat()
    return {
        'Id': {
            'S': str(id)
        },
        'DateAdded': {
            'S': str(now)
        },
        'Name': {
            'S': str(name)
        },
        'Details': {
            'S': str(details)
        }
    }


def _put_dynamo_item(id, name, details=''):
    return dynamodb.put_item(TableName='OrdServerTable', Item=_build_dynamo_item(id, name, details))

def _popen(cmd):
    return subprocess.Popen([cmd], shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def _cmd(cmd):
    try:
        proc = _popen(cmd)
    except FileNotFoundError:
        print('_cmd FileNotFoundError')
        if len(error): error += '\n'
        error += f'error: FileNotFound for {cmd}. This happens if the base program is still in the build process.'
        return None, error
    except Exception as e:
        print(f'_cmd Exception: {e}')
        if len(error): error += '\n'
        error += f'error: another exception occurred: {e}'
        return None, error

    # TODO : below this line should be separated into a method like "_make_subprocess_output_readable"
    out = ''
    errors = ''
    out_raw = proc.stdout.readlines()
    error_raw = proc.stderr.readlines()

    for line in out_raw:
        try:
            out += line.decode('ascii')
        except:
            out += str(line)

    for line in error_raw:
        try:
            out += line.decode('ascii')
        except:
            out += str(line)
        out += '\n'

    return out, errors


async def echo(websocket):
    async for message in websocket:
        await websocket.send(message)


async def exec(websocket):
    client_token = await websocket.recv()
    client_token = client_token.split('token:')[1]
    if token == client_token:
        CLIENTS.add(websocket)
        async for message in websocket:
            print(f'websocket recvd {message}')
            output = None
            try:
                if message == 'websocket restart':
                    subprocess.Popen(['sudo', 'systemctl', 'restart', 'ord-controller.service'], shell=True, stdout=subprocess.PIPE)
                elif message == 'bitcoind restart':
                    subprocess.Popen(['sudo','systemctl', 'restart', 'bitcoin-for-ord.service'], shell=True, stdout=subprocess.PIPE)
                elif message == 'ord index restart':
                    subprocess.Popen(['sudo','systemctl', 'restart', 'ord.service'], shell=True, stdout=subprocess.PIPE)
                elif message == 'restart restart':
                    subprocess.Popen(['sudo shutdown -r now'], shell=True, stdout=subprocess.PIPE)
                await websocket.send(output)
            except Exception as e:
                print(f'Exception: {e}')
                await websocket.send(f'Exception: {e}')
    else:
        await websocket.close(1011, "authentication failed")
        return


def get_ps_as_dicts(ps_output):
    output = []
    headers = [h for h in ' '.join(ps_output[0].decode('ascii').strip().split()).split() if h]
    if len(ps_output) > 1:
        for row in ps_output[1:]:
            values = row.decode('ascii').replace('\n', '').split(None, len(headers) - 1)
            output.append({headers[i]: values[i] for i in range(len(headers))})
    return output


ord_index_output = []

def get_ord_indexing_details():
    global ord_index_output
    ord_index_output.append('looking for ord index...')
    ps = _popen('ps aux | head -1; ps aux | grep "[/]home/ubuntu/ord/target/release/ord"').stdout.readlines()
    output = get_ps_as_dicts(ps)
    if len(output):
        pid = output[0]['PID']
        ord_index_output.append(f'found PID {pid}')
        # see https://stackoverflow.com/questions/54091396/live-output-stream-from-python-subprocess/54091788#54091788
        with _popen(f'sudo strace -qfp  {pid} -e trace=write -e write=1,2') as process:
            ord_index_output.append('running strace...')
            for line in process.stdout:
                line_txt = line.decode('ascii')
                ord_index_output.append(line_txt)
                now = datetime.utcnow().isoformat()
                item = {
                    'Id': {
                        'S': f'Ord_Index_Output_{now}'
                    },
                    'DateAdded': {
                        'S': now
                    },
                    'Name': {
                        'S': 'Ord_Index_Output'
                    },
                    'Details': {
                        'S': line_txt
                    }
                }
                if not ec2_credentials_failure:
                    dynamodb.put_item(TableName='OrdServerTable', Item=item)

def get_ord_indexing_output():
    global ord_index_output
    return json.dumps({"ord_index_output": ord_index_output})


def get_ord_index_service_status():
    output, error = _cmd('journalctl -r -u ord.service')
    return json.dumps({
        "ord_index_service_status": output,
        "ord_index_service_status_error": error
    })


def get_bitcoind_status():
    pid = 99999999
    pid_search = _popen("systemctl status bitcoin-for-ord.service | grep 'Main PID' | awk '{print $3}'").stdout.readlines()

    if len(pid_search) == 1:
        pid = pid_search[0].decode('ascii').replace('\n', '')
    ps = _popen(f'ps -p {pid} -o user,pid,ppid,%cpu,%mem,vsz,rss,tty,stat,start,time,command').stdout.readlines()
    output = {"bitcoind_status": get_ps_as_dicts(ps)}

    return json.dumps(output)


def get_ord_wallet():
    ord_wallet = {}

    # wallet help
    if 'help' not in ord_wallet:
        ord_wallet['help'] = _cmd(f'{ord_command} wallet help')
    
    # inscriptions
    if 'inscriptions' not in ord_wallet:
        output, error = _cmd(f'{ord_command} wallet inscriptions')
        ord_wallet['inscriptions'] = output if output else error
        if len(error):
            ord_wallet['inscriptions_error'] = error
    return json.dumps({"ord_wallet": ord_wallet})


def get_journalctl_alerts():
    output, error = _cmd('journalctl -r -p 0..4')
    return json.dumps({
        'journalctl_alerts': output,
        'journalctl_errors': error
    })
    

async def broadcast(message):
    for websocket in CLIENTS.copy():
        try:
            await websocket.send(message)
        except websockets.ConnectionClosed:
            pass


async def broadcast_messages():
    global ord_index_output
    while True:
        await broadcast(get_bitcoind_status())
        await broadcast(get_ord_index_service_status())
        await broadcast(get_ord_wallet())
        await broadcast(get_ord_indexing_output())
        await broadcast(get_journalctl_alerts())
        print(f'ord_index_output is {ord_index_output}')

        if ec2_credentials_failure:
            await broadcast(json.dumps({
                'boto3_credentials_not_found': True
            }))

        await asyncio.sleep(5)


async def main():
    async with websockets.serve(exec, "0.0.0.0", 8765):
        # await asyncio.Future()  # run forever
        await broadcast_messages()  # runs forever


def record_init():
    global ec2_credentials_failure
    now = datetime.utcnow().isoformat()
    try:
        _put_dynamo_item(now, 'Init')
    except NoCredentialsError as e:
        # TODO: why can't i find more info on this intermittent problem b/w boto3 and ec2?
        print('boto3 could not get ec2 credentials')
        ec2_credentials_failure = True


record_init()
t1 = Thread(target=get_ord_indexing_details)
t1.start()

asyncio.run(main())
