#%%
from fastai.text import *
from data_helpers.Data import *
from fastai.text.transform import *

hyper_params = {
    "max_sequence_length": 40*70,
    "batch_size": 4,
    "num_epochs1": 12,
    "num_epochs2": 15,
    "num_aspect": 6,
    "num_rating": 5,
}


#%%
cls_db = load_data("./data/", "cls_databunch_hotel.allaspect.1115")

#%%
cls_db.batch_size=hyper_params["batch_size"]
cls_db.batch_size


#%%
class SentenceEncoder(Module):
    "Create an encoder over `module` that can process a full sentence."
    def __init__(self, bptt:int, max_len:int, module:nn.Module, vocab, pad_idx:int=1):
        print("Encoder init")
        self.max_len,self.bptt,self.module,self.pad_idx = max_len,bptt,module,pad_idx
        self.vocab = vocab
        self.period_index = self.vocab.stoi["xxperiod"]

    def concat(self, arrs:Collection[Tensor])->Tensor:
        "Concatenate the `arrs` along the batch dimension."
        return [torch.cat([l[si] for l in arrs], dim=1) for si in range_of(arrs[0])]

    def reset(self):
        if hasattr(self.module, 'reset'): self.module.reset()

    def forward(self, input:LongTensor)->Tuple[Tensor,Tensor]:
        bs,sl = input.size()
        self.reset()
        raw_outputs,outputs,masks = [],[],[]
        p_index = []
        for i in range(0, sl, self.bptt):
            r, o = self.module(input[:,i: min(i+self.bptt, sl)])
            if i>(sl-self.max_len):
                masks.append(input[:,i: min(i+self.bptt, sl)] == self.pad_idx)
                raw_outputs.append(r)
                outputs.append(o)
                p_index.append( input[:,i: min(i+self.bptt, sl)] == self.period_index )

                
        # print("number of sentences in docs:")
#         n_sent = torch.sum( x==self.vocab.stoi["xxperiod"] , dim=1)
        # print(n_sent)
        
        # print("locating period marks")
        period_index = torch.cat(p_index,dim=1)
        
        return self.concat(raw_outputs),self.concat(outputs), \
               torch.cat(masks,dim=1),period_index

#%%
def pool_combo(output, start, end):
    avg_pool = output[start:end, :].mean(dim=0)
    max_pool = output[start:end, :].max(dim=0)[0]
    x = torch.cat([output[-1,:], max_pool, avg_pool], 0)
    return x

def sentence_pool_1200(outputs, mask, p_index):
    "Pool MultiBatchEncoder outputs into one vector [last_hidden, max_pool, avg_pool]."
    output = outputs[-1]
    seq_max = output.size(1)
    doc_start = mask.int().sum(dim=1)
    
    batch = []
    for doci in range(0,output.shape[0]):
        pi = p_index[doci,:].nonzero(as_tuple=True)[0].int()
        doc = []
        for senti in range( len(pi) ):
            if senti==0:
                doc.append( pool_combo(output[doci,:,:], doc_start[doci], pi[senti]) )
            else:
                doc.append( pool_combo(output[doci,:,:], pi[senti-1]+1, pi[senti]) )
            
        batch.append( torch.stack(doc, 0) )

    return batch

def masked_concat_pool(outputs, mask):
    "Pool MultiBatchEncoder outputs into one vector [last_hidden, max_pool, avg_pool]."
    output = outputs[-1]
    avg_pool = output.masked_fill(mask[:, :, None], 0).mean(dim=1)
    avg_pool *= output.size(1) / (output.size(1)-mask.type(avg_pool.dtype).sum(dim=1))[:,None]
    max_pool = output.masked_fill(mask[:,:,None], -float('inf')).max(dim=1)[0]
    x = torch.cat([output[:,-1], max_pool, avg_pool], 1)
    return x

#%%

def sentence_extract_pool(outputs, mask, p_index):
    output = outputs[-1]
    
    batch = []
    for doci in range(0,output.shape[0]):
        doc = output[doci,p_index[doci,:],:]
        batch.append( doc )

    return batch

class Cls1ATT400(Module):
    "Create a linear classifier with pooling."
    def __init__(self, n_asp:int, n_rat:int, layers:Collection[int], drops:Collection[float]):
        print("CLS init")
        print("Num Aspect: "+str(n_asp) )
        print("Num Rating: "+str(n_rat) )
        self.n_asp = n_asp
        self.n_rat = n_rat
        
        mod_layers = []
        mod_layers += bn_drop_lin( 1200, 50, p=0.5, actn=nn.ReLU(inplace=True) )
        mod_layers += bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) )
        self.overall = nn.Sequential(*mod_layers)
        
        mod_layers = []
        mod_layers += bn_drop_lin( 400, 50, p=0.5, actn=nn.ReLU(inplace=True) )
#         mod_layers += bn_drop_lin( 50, self.n_asp, p=0, actn=torch.nn.Softmax(dim=1) )
        mod_layers += bn_drop_lin( 50, self.n_asp, p=0, actn=torch.nn.Sigmoid() )
        self.aspect = nn.Sequential(*mod_layers)
        
        mod_layers = []
        mod_layers += bn_drop_lin( 400, 50, p=0.5, actn=nn.ReLU(inplace=True) )
        mod_layers += bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) )
        self.sentiment = nn.Sequential(*mod_layers)

    def forward(self, input:Tuple[Tensor,Tensor,Tensor,Tensor])->Tuple[Tensor,Tensor,Tensor]:
        raw_outputs,outputs,mask,p_index = input
        
        doc_batch = masked_concat_pool(outputs, mask)
        overall_dist = self.overall(doc_batch)
        
        batch = sentence_extract_pool(outputs, mask, p_index)
        
        allsent_emb = torch.cat(batch, dim=0)          # [n_sentence, emb400]
        aspect_dist = self.aspect(allsent_emb)         # [n_sentence, aspect6]

        sent_bmm = torch.bmm(aspect_dist.unsqueeze(2), allsent_emb.unsqueeze(1))  # [319, 6, 400]
        
        all_doc_emb = []
        aspect_doc = []
        sentim_doc = []
        cur = 0
        for doci in range(0, len(batch)):
            sn = batch[doci].shape[0]
            doc_emb = torch.sum(sent_bmm[cur:(cur+sn), :, : ], dim=0, keepdim=True) # [1, 6, 400]
            asp_w_sum = torch.sum(aspect_dist[cur:(cur+sn),:], dim=0, keepdim=True) # [1, 6]
            doc_emb = doc_emb / asp_w_sum[:,:,None]                                 # [1, 6, 400]

            all_doc_emb.append(doc_emb)
            aspect_doc.append( aspect_dist[cur:(cur+sn), :] )
            
            cur = cur + sn

        all_doc_emb = torch.cat( all_doc_emb, dim=0 )
        result = self.sentiment( all_doc_emb.view(-1, 400) )  # [batch*asp, sentiment5]
        result = result.view(-1, self.n_asp, self.n_rat)
        
        result = torch.cat( [overall_dist[:,None,:], result], dim=1 )
        
        return result,raw_outputs,outputs,aspect_doc


#%%
class Cls1ATT400_DOCONLY(Module):
    "Create a linear classifier with pooling."
    def __init__(self, n_asp:int, n_rat:int, layers:Collection[int], drops:Collection[float]):
        print("CLS init")
        print("Num Aspect: "+str(n_asp) )
        print("Num Rating: "+str(n_rat) )
        self.n_asp = n_asp
        self.n_rat = n_rat
        
        mod_layers = []
        mod_layers += bn_drop_lin( 1200, 50, p=0.5, actn=nn.ReLU(inplace=True) )
        mod_layers += bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) )
        self.overall = nn.Sequential(*mod_layers)
        
        mod_layers = []
        mod_layers += bn_drop_lin( 1200, 50, p=0.5, actn=nn.ReLU(inplace=True) )
#         mod_layers += bn_drop_lin( 50, self.n_asp, p=0, actn=torch.nn.Softmax(dim=1) )
        mod_layers += bn_drop_lin( 50, self.n_asp, p=0, actn=None )
        self.aspect = nn.Sequential(*mod_layers)
        
        mod_layers = []
        mod_layers += bn_drop_lin( 1200, 50, p=0.5, actn=nn.ReLU(inplace=True) )
        mod_layers += bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) )
        self.sentiment = nn.Sequential(*mod_layers)

    def forward(self, input:Tuple[Tensor,Tensor,Tensor,Tensor])->Tuple[Tensor,Tensor,Tensor]:
        raw_outputs,outputs,mask,p_index = input
        
        doc_batch = masked_concat_pool(outputs, mask)
        overall_dist = self.overall(doc_batch)
        
        batch = sentence_extract_pool(outputs, mask, p_index)
        
        allsent_emb = torch.cat(batch, dim=0)          # [n_sentence, emb400]
        aspect_dist = self.aspect(allsent_emb)         # [n_sentence, aspect6]
        
        doc_norm_aspect_dist = []
        cur = 0
        for doci in range(0, len(batch)):
            sn = batch[doci].shape[0]
            doc_norm_aspect_dist.append( torch.nn.functional.softmax(aspect_dist[cur:(cur+sn),:], dim=0) )
            cur = cur + sn
        doc_norm_aspect_dist = torch.cat( doc_norm_aspect_dist, dim=0 )

        sent_bmm = torch.bmm(doc_norm_aspect_dist.unsqueeze(2), allsent_emb.unsqueeze(1))  # [319, 6, 400]
        
        all_doc_emb = []
        aspect_doc = []
        sentim_doc = []
        cur = 0
        for doci in range(0, len(batch)):
            sn = batch[doci].shape[0]
            doc_emb = torch.sum(sent_bmm[cur:(cur+sn), :, : ], dim=0, keepdim=True) # [1, 6, 400]
#             asp_w_sum = torch.sum(aspect_dist[cur:(cur+sn),:], dim=0, keepdim=True) # [1, 6]
#             doc_emb = doc_emb / asp_w_sum[:,:,None]                                 # [1, 6, 400]

            all_doc_emb.append(doc_emb)
            aspect_doc.append( doc_norm_aspect_dist[cur:(cur+sn), :] )
            
            cur = cur + sn

        all_doc_emb = torch.cat( all_doc_emb, dim=0 )
        result = self.sentiment( all_doc_emb.view(-1, 1200) )  # [batch*asp, sentiment5]
        result = result.view(-1, self.n_asp, self.n_rat)
        
        result = torch.cat( [overall_dist[:,None,:], result], dim=1 )
        
        return result,raw_outputs,outputs,aspect_doc

#%%

class Cls1ATT400_INDI(Module):
    "Create a linear classifier with pooling."
    def __init__(self, n_asp:int, n_rat:int, layers:Collection[int], drops:Collection[float]):
        print("CLS init")
        print("Num Aspect: "+str(n_asp) )
        print("Num Rating: "+str(n_rat) )
        self.n_asp = n_asp
        self.n_rat = n_rat
        
        mod_layers = []
        mod_layers += bn_drop_lin( 1200, 50, p=0.5, actn=nn.ReLU(inplace=True) )
        mod_layers += bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) )
        self.overall = nn.Sequential(*mod_layers)
        
        mod_layers = []
        mod_layers += bn_drop_lin( 1200, 50, p=0.5, actn=nn.ReLU(inplace=True) )
        mod_layers += bn_drop_lin( 50, self.n_asp, p=0, actn=torch.nn.Softmax(dim=1) )
#         mod_layers += bn_drop_lin( 50, self.n_asp, p=0, actn=None )
        self.aspect = nn.Sequential(*mod_layers)
        
        self.senti_base = nn.Sequential(*bn_drop_lin( 1200, 50, p=0.5, actn=nn.ReLU(inplace=True) ) )
        
        self.s1 = nn.Sequential(* bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) ) )
        self.s2 = nn.Sequential(* bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) ) )
        self.s3 = nn.Sequential(* bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) ) )
        self.s4 = nn.Sequential(* bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) ) )
        self.s5 = nn.Sequential(* bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) ) )
        self.s6 = nn.Sequential(* bn_drop_lin( 50, self.n_rat, p=0, actn=torch.nn.Softmax(dim=1) ) )
        self.sentiments = []
        self.sentiments += self.s1
        self.sentiments += self.s2
        self.sentiments += self.s3
        self.sentiments += self.s4
        self.sentiments += self.s5
        self.sentiments += self.s6

    def forward(self, input:Tuple[Tensor,Tensor,Tensor,Tensor])->Tuple[Tensor,Tensor,Tensor]:
        raw_outputs,outputs,mask,p_index = input
        
        doc_batch = masked_concat_pool(outputs, mask)
        overall_dist = self.overall(doc_batch)
        
        batch = sentence_pool_1200(outputs, mask, p_index)
        
        allsent_emb = torch.cat(batch, dim=0)          # [n_sentence, emb400]
        aspect_dist = self.aspect(allsent_emb)         # [n_sentence, aspect6]

        sent_bmm = torch.bmm(aspect_dist.unsqueeze(2), allsent_emb.unsqueeze(1))  # [319, 6, 400]
        
        all_doc_emb = []
        aspect_doc = []
        sentim_doc = []
        cur = 0
        for doci in range(0, len(batch)):
            sn = batch[doci].shape[0]
            doc_emb = torch.sum(sent_bmm[cur:(cur+sn), :, : ], dim=0, keepdim=True) # [1, 6, 400]
            asp_w_sum = torch.sum(aspect_dist[cur:(cur+sn),:], dim=0, keepdim=True) # [1, 6]
            doc_emb = doc_emb / asp_w_sum[:,:,None]                                 # [1, 6, 400]

            all_doc_emb.append(doc_emb)
            aspect_doc.append( aspect_dist[cur:(cur+sn), :] )
            
            cur = cur + sn

        all_doc_emb = torch.cat( all_doc_emb, dim=0 )          # [batch, asp, 1200]
        
        result_senti_base = self.senti_base( all_doc_emb.view(-1, 1200) ) # [batch*asp, 50]
        result_senti_base = result_senti_base.view(-1, self.n_asp, 50)    # [batch, asp, 50]
        
        result_senti = [ self.sentiments[aspi]( result_senti_base[:,aspi,:] ) for aspi in range(0,self.n_asp)] # [batch, ra]
        
        result = torch.stack(result_senti, dim=1)  # [batch, asp, sentiment5]
        
        result = torch.cat( [overall_dist[:,None,:], result], dim=1 )
        
        return result,raw_outputs,outputs,aspect_doc


#%%
def get_text_classifier(arch:Callable, vocab_sz:int, vocab, n_class:int, bptt:int=70, max_len:int=20*70, config:dict=None,
                        drop_mult:float=1., lin_ftrs:Collection[int]=None, ps:Collection[float]=None,
                        pad_idx:int=1) -> nn.Module:
    "Create a text classifier from `arch` and its `config`, maybe `pretrained`."
    print("CUSTOM DEFINED CLASSIFIER")
    meta = text.learner._model_meta[arch]
    config = ifnone(config, meta['config_clas']).copy()
    for k in config.keys():
        if k.endswith('_p'): config[k] *= drop_mult
    if lin_ftrs is None: lin_ftrs = [50]
    if ps is None:  ps = [0.1]*len(lin_ftrs)
    layers = [config[meta['hid_name']] * 3] + lin_ftrs + [n_class]
    ps = [config.pop('output_p')] + ps
    init = config.pop('init') if 'init' in config else None
    encoder = SentenceEncoder(bptt, max_len, arch(vocab_sz, **config), vocab, pad_idx=pad_idx)
    cls_layer = Cls1ATT400_INDI(n_asp=6, n_rat=5, layers=layers, drops=ps)
    model = SequentialRNN(encoder, cls_layer)
    return model if init is None else model.apply(init)

def text_classifier_learner(data:DataBunch, arch:Callable, bptt:int=70, max_len:int=20*70, config:dict=None,
                            pretrained:bool=True, drop_mult:float=1., lin_ftrs:Collection[int]=None,
                            ps:Collection[float]=None, **learn_kwargs) -> 'TextClassifierLearner':
    "Create a `Learner` with a text classifier from `data` and `arch`."
    model = get_text_classifier(arch, len(data.vocab.itos), data.vocab, data.c, bptt=bptt, max_len=max_len,
                                config=config, drop_mult=drop_mult, lin_ftrs=lin_ftrs, ps=ps)
    meta = text.learner._model_meta[arch]
    learn = RNNLearner(data, model, split_func=meta['split_clas'], **learn_kwargs)
    if pretrained:
        if 'url' not in meta:
            warn("There are no pretrained weights for that architecture yet!")
            return learn
        model_path = untar_data(meta['url'], data=False)
        fnames = [list(model_path.glob(f'*.{ext}'))[0] for ext in ['pth', 'pkl']]
        learn = learn.load_pretrained(*fnames, strict=False)
        learn.freeze()
    return learn

#%%
class MultiLabelCEL(nn.CrossEntropyLoss):
    def forward(self, input, target):
        # print("in multi label cel")
        # print(input.shape)
        # print(target.shape)
        
        target = target.long()

        loss = 0
        for i in range(6):
            loss = loss + super(MultiLabelCEL, self).forward(input[:,i,:], target[:,i])
        
        return loss


#%%
mloss = MultiLabelCEL()
cls_learn = text_classifier_learner(cls_db, AWD_LSTM, 
                                    loss_func=mloss,
                                    max_len=hyper_params["max_sequence_length"])


#%%
x,y = cls_db.one_batch()


# %%
x.shape

# %%
enc = cls_learn.model[0]
enc

# %%
raw,output,mask,pindex = enc(x.cuda())

# %%
output[-1].shape

# %%
dec = cls_learn.model[1]
dec

# %%
pred = dec([raw,output,mask,pindex])
pred

# %%
