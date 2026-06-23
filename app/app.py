import os
import zipfile
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, send_from_directory
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter, Transformation
from PIL import Image
import io

from models import db, User, Document, AuditLog

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'uploads')
app.config['OUTPUT_FOLDER'] = os.path.join(os.getcwd(), 'output')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

MARGEM_X = 40
MARGEM_Y = 40
LARGURA_ASSINATURA = 160

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def criar_assinatura_pdf(img_bytes, w, h, position):
    """
    position: 'left' ou 'right'
    """
    temp_buffer = io.BytesIO()
    img = Image.open(io.BytesIO(img_bytes))
    proporcao = img.height / img.width
    altura = LARGURA_ASSINATURA * proporcao

    c = canvas.Canvas(temp_buffer, pagesize=(w, h))
    
    if position == 'right':
        x = w - LARGURA_ASSINATURA - MARGEM_X
    else: # left
        x = MARGEM_X
        
    # Salva a imagem temporariamente para o reportlab (ele prefere caminhos ou objetos parecidos com arquivos)
    img_temp_path = "temp_sig.png"
    img.save(img_temp_path)
    
    c.drawImage(img_temp_path, x, MARGEM_Y,
                width=LARGURA_ASSINATURA, height=altura, mask="auto")
    c.save()
    os.remove(img_temp_path)
    temp_buffer.seek(0)
    return temp_buffer

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Usuário ou senha inválidos')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    if current_user.role == 'admin':
        docs = Document.query.all()
        stats = {
            'total': len(docs),
            'pending': len([d for d in docs if d.status == 'PENDING']),
            'in_progress': len([d for d in docs if d.status == 'IN_PROGRESS']),
            'completed': len([d for d in docs if d.status == 'COMPLETED'])
        }
        return render_template('admin_dashboard.html', docs=docs, stats=stats)
    else:
        docs = Document.query.filter_by(current_signer_id=current_user.id, status='IN_PROGRESS').all()
        history = AuditLog.query.filter_by(user_id=current_user.id).all()
        return render_template('signer_dashboard.html', docs=docs, history=history)

@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
    
    workflow = request.form.get('workflow')
    files = request.files.getlist('pdfs')
    
    for file in files:
        if file and file.filename.endswith('.pdf'):
            filename = secure_filename(f"{datetime.now().timestamp()}_{file.filename}")
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            # Determinar primeiro assinador baseado no fluxo
            # Fluxo A: Admin -> Signer 1
            # Fluxo B: Admin -> Signer 2 -> Signer 1
            if workflow == 'A':
                first_signer = User.query.filter_by(role='signer1').first()
            else:
                first_signer = User.query.filter_by(role='signer2').first()
            
            new_doc = Document(
                filename=filename,
                original_filename=file.filename,
                workflow_type=workflow,
                status='IN_PROGRESS',
                current_signer_id=first_signer.id if first_signer else None
            )
            db.session.add(new_doc)
            db.session.flush()
            
            log = AuditLog(document_id=new_doc.id, user_id=current_user.id, action=f"Upload realizado (Fluxo {workflow})")
            db.session.add(log)
            
    db.session.commit()
    flash('Documentos enviados com sucesso!')
    return redirect(url_for('dashboard'))

@app.route('/sign/<int:doc_id>', methods=['POST'])
@login_required
def sign(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if doc.current_signer_id != current_user.id:
        flash('Você não tem permissão para assinar este documento.')
        return redirect(url_for('dashboard'))
    
    sig_file = request.files.get('signature')
    if not sig_file:
        flash('Por favor, envie uma imagem de assinatura.')
        return redirect(url_for('dashboard'))
    
    sig_bytes = sig_file.read()
    
    # Processar PDF
    input_path = os.path.join(app.config['UPLOAD_FOLDER'], doc.filename)
    # Usar um nome temporário para não sobrescrever enquanto lê
    temp_output_filename = f"signed_{doc.filename}"
    output_path = os.path.join(app.config['UPLOAD_FOLDER'], temp_output_filename)
    
    reader = PdfReader(input_path)
    writer = PdfWriter()
    
    first_page = reader.pages[0]
    w = float(first_page.mediabox.width)
    h = float(first_page.mediabox.height)
    
    # Posição: Signer 2 (Left), Signer 1 (Right)
    pos = 'left' if current_user.role == 'signer2' else 'right'
    
    sig_pdf_buffer = criar_assinatura_pdf(sig_bytes, w, h, pos)
    sig_page = PdfReader(sig_pdf_buffer).pages[0]
    
    for p in reader.pages:
        p.merge_transformed_page(sig_page, Transformation())
        writer.add_page(p)
        
    with open(output_path, "wb") as f:
        writer.write(f)
    
    # Atualizar arquivo original pelo assinado
    os.replace(output_path, input_path)
    
    # Lógica de Fluxo
    log_action = f"Assinado por {current_user.username}"
    if doc.workflow_type == 'B' and current_user.role == 'signer2':
        # Avança para Signer 1
        next_signer = User.query.filter_by(role='signer1').first()
        doc.current_signer_id = next_signer.id if next_signer else None
    else:
        # Finalizado
        doc.status = 'COMPLETED'
        doc.current_signer_id = None
        # Mover para pasta de output final
        final_path = os.path.join(app.config['OUTPUT_FOLDER'], doc.filename)
        os.rename(input_path, final_path)
        log_action += " - Fluxo Concluído"
        
    log = AuditLog(document_id=doc.id, user_id=current_user.id, action=log_action)
    db.session.add(log)
    db.session.commit()
    
    flash('Documento assinado com sucesso!')
    return redirect(url_for('dashboard'))

@app.route('/download/<int:doc_id>')
@login_required
def download_single(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if doc.status != 'COMPLETED' and current_user.role != 'admin':
        flash('Acesso negado.')
        return redirect(url_for('dashboard'))
    
    folder = app.config['OUTPUT_FOLDER'] if doc.status == 'COMPLETED' else app.config['UPLOAD_FOLDER']
    return send_from_directory(folder, doc.filename, as_attachment=True, download_name=doc.original_filename)

@app.route('/download_batch', methods=['POST'])
@login_required
def download_batch():
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
    
    doc_ids = request.form.getlist('doc_ids')
    if not doc_ids:
        # Se não houver IDs selecionados, mas clicaram em "Baixar Todos"
        if 'download_all' in request.form:
            docs = Document.query.filter_by(status='COMPLETED').all()
            doc_ids = [str(d.id) for d in docs]
        else:
            flash('Nenhum documento selecionado.')
            return redirect(url_for('dashboard'))
    
    zip_filename = f"documentos_concluidos_{datetime.now().strftime('%Y%m%d%H%M%S')}.zip"
    zip_path = os.path.join(app.config['OUTPUT_FOLDER'], zip_filename)
    
    with zipfile.ZipFile(zip_path, 'w') as z:
        for d_id in doc_ids:
            doc = Document.query.get(int(d_id))
            if doc and doc.status == 'COMPLETED':
                file_path = os.path.join(app.config['OUTPUT_FOLDER'], doc.filename)
                if os.path.exists(file_path):
                    z.write(file_path, doc.original_filename)
                    
    return send_file(zip_path, as_attachment=True)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        # Criar usuários iniciais se não existirem
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', role='admin')
            admin.set_password('admin123')
            s1 = User(username='assinador1', role='signer1')
            s1.set_password('senha1')
            s2 = User(username='assinador2', role='signer2')
            s2.set_password('senha2')
            db.session.add_all([admin, s1, s2])
            db.session.commit()
            
    app.run(host='0.0.0.0', port=10000, debug=True)
