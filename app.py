from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from pyzabbix import ZabbixAPI
import pandas as pd
import os
from werkzeug.utils import secure_filename
import json
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'sua_chave_secreta_aqui_mude_em_producao'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

# Configurações do Zabbix
ZABBIX_URL = 'http://10.3.5.34/zabbix/api_jsonrpc.php'
ZABBIX_USER = 'Admin'
ZABBIX_PASSWORD = 'zabbix'

# Garantir que a pasta de uploads existe
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Conexão global com Zabbix (será inicializada por requisição)
zabbix = None

def get_zabbix_connection():
    """Estabelece conexão com o Zabbix"""
    global zabbix
    try:
        if zabbix is None:
            zabbix = ZabbixAPI(ZABBIX_URL)
            zabbix.login(ZABBIX_USER, ZABBIX_PASSWORD)
            print(f"Conectado ao Zabbix API versão {zabbix.api_version()}")
        return zabbix
    except Exception as e:
        print(f"Erro ao conectar ao Zabbix: {str(e)}")
        return None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_templates', methods=['GET'])
def get_templates():
    """Endpoint para buscar templates do Zabbix"""
    try:
        zapi = get_zabbix_connection()
        if not zapi:
            return jsonify({'error': 'Não foi possível conectar ao Zabbix'}), 500
        
        # Buscar templates
        templates = zapi.template.get(
            output=['templateid', 'name'],
            filter={'host': ''}  # Pega todos os templates
        )
        
        # Formatar para o select2
        template_list = [{'id': t['templateid'], 'text': t['name']} for t in templates]
        
        return jsonify(template_list)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/import', methods=['POST'])
def import_hosts():
    """Endpoint para importar hosts do CSV"""
    try:
        # Verificar se arquivo foi enviado
        if 'csv_file' not in request.files:
            flash('Nenhum arquivo enviado', 'error')
            return redirect(url_for('index'))
        
        file = request.files['csv_file']
        if file.filename == '':
            flash('Nenhum arquivo selecionado', 'error')
            return redirect(url_for('index'))
        
        if not file.filename.endswith('.csv'):
            flash('Por favor, envie um arquivo CSV', 'error')
            return redirect(url_for('index'))
        
        # Salvar arquivo temporariamente
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        # Ler CSV
        df = pd.read_csv(filepath)
        
        # Validar colunas necessárias
        required_columns = ['nome', 'ip']
        if not all(col in df.columns for col in required_columns):
            flash('O CSV deve conter as colunas: nome, ip', 'error')
            os.remove(filepath)
            return redirect(url_for('index'))
        
        # Obter parâmetros do formulário
        template_ids = request.form.getlist('templates[]')
        snmp_version = request.form.get('snmp_version')
        
        if not template_ids:
            flash('Selecione pelo menos um template', 'error')
            os.remove(filepath)
            return redirect(url_for('index'))
        
        # Conectar ao Zabbix
        zapi = get_zabbix_connection()
        if not zapi:
            flash('Erro ao conectar ao Zabbix', 'error')
            os.remove(filepath)
            return redirect(url_for('index'))
        
        # Processar cada host
        results = []
        for index, row in df.iterrows():
            result = create_host_in_zabbix(
                zapi, 
                row['nome'], 
                row['ip'], 
                template_ids,
                snmp_version,
                request.form
            )
            results.append(result)
        
        # Remover arquivo temporário
        os.remove(filepath)
        
        # Preparar resumo
        success_count = sum(1 for r in results if r['status'] == 'success')
        error_count = len(results) - success_count
        
        return render_template('index.html', 
                             results=results,
                             success_count=success_count,
                             error_count=error_count)
        
    except Exception as e:
        flash(f'Erro ao processar importação: {str(e)}', 'error')
        return redirect(url_for('index'))

def create_host_in_zabbix(zapi, hostname, ip, template_ids, snmp_version, form_data):
    """Cria um host no Zabbix com as configurações especificadas"""
    try:
        # Verificar se host já existe
        existing_host = zapi.host.get(filter={'host': hostname})
        if existing_host:
            return {
                'nome': hostname,
                'ip': ip,
                'status': 'error',
                'mensagem': f'Host {hostname} já existe'
            }
        
        # Preparar interface SNMP
        interface = {
            'type': 2,  # SNMP
            'main': 1,
            'useip': 1,
            'ip': ip,
            'dns': '',
            'port': '161',
            'details': {
                'version': snmp_version,
                'bulk': 0
            }
        }
        
        # Configurar detalhes SNMP baseado na versão
        if snmp_version == '2':
            interface['details']['community'] = form_data.get('snmp_community', 'public')
        elif snmp_version == '3':
            interface['details']['securityname'] = form_data.get('snmp_securityname', '')
            interface['details']['securitylevel'] = int(form_data.get('snmp_securitylevel', 0))
            interface['details']['authprotocol'] = form_data.get('snmp_authprotocol', '0')
            interface['details']['authpassphrase'] = form_data.get('snmp_authpassphrase', '')
            interface['details']['privprotocol'] = form_data.get('snmp_privprotocol', '0')
            interface['details']['privpassphrase'] = form_data.get('snmp_privpassphrase', '')
            interface['details']['contextname'] = form_data.get('snmp_contextname', '')
        
        # Criar host
        create_params = {
            'host': hostname,
            'name': hostname,
            'interfaces': [interface],
            'groups': [{'groupid': get_default_hostgroup(zapi)}],
            'templates': [{'templateid': tid} for tid in template_ids],
            'status': 0  # 0 = enabled, 1 = disabled
        }
        
        result = zapi.host.create(create_params)
        
        return {
            'nome': hostname,
            'ip': ip,
            'status': 'success',
            'mensagem': f'Host criado com sucesso (ID: {result["hostids"][0]})'
        }
        
    except Exception as e:
        return {
            'nome': hostname,
            'ip': ip,
            'status': 'error',
            'mensagem': str(e)
        }

def get_default_hostgroup(zapi):
    """Obtém ou cria um grupo padrão para hosts"""
    try:
        groups = zapi.hostgroup.get(filter={'name': 'Imported Hosts'})
        if groups:
            return groups[0]['groupid']
        
        # Criar grupo se não existir
        result = zapi.hostgroup.create({'name': 'Imported Hosts'})
        return result['groupids'][0]
    except:
        # Se falhar, pegar qualquer grupo existente
        groups = zapi.hostgroup.get()
        return groups[0]['groupid'] if groups else None

@app.route('/test_connection', methods=['GET'])
def test_connection():
    """Testa conexão com o Zabbix"""
    try:
        zapi = get_zabbix_connection()
        if zapi:
            return jsonify({
                'status': 'success',
                'message': f'Conectado ao Zabbix {zapi.api_version()}'
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'Não foi possível conectar ao Zabbix'
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5010)
